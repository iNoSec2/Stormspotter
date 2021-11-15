import asyncio
import logging
import time
from dataclasses import dataclass
from itertools import chain
from typing import Any, Dict

import aiohttp

from .context import CollectorContext
from .utils import sqlite_writer

log = logging.getLogger("rich")


class _TokenEvent(asyncio.Event):
    """Handles manual refreshing of access tokens during AAD enumeration"""

    def __init__(self, ctx: CollectorContext, base_url: str, objName: str) -> None:
        super().__init__()
        self.currentToken = None
        self.token_refresh_task = asyncio.create_task(
            self._get_new_token_for_aad_enum(ctx, base_url, objName)
        )

    async def _get_new_token_for_aad_enum(
        self, ctx: CollectorContext, base_url: str, objName: str
    ):
        """Background task to get new token before access token expiration."""
        while True:
            self.currentToken = await ctx.cred.get_token(base_url + "/.default")

            # Set event to resume enumeration
            self.set()

            # Stop enumeration 15 seconds before token set to expire
            now = int(time.time())
            await asyncio.sleep(self.currentToken.expires_on - now - 15)

            # Prevent requests by clearing event
            self.clear()
            log.info(f"Waiting for new access tokens for {objName} enumeration...")

            # Check to see if expiration has passed yet. Refresh after expiration to be safe.
            scope = base_url + "/.default"
            self.currentToken = await ctx.cred.get_token(scope)
            while self.currentToken.expires_on < int(time.time()):
                await asyncio.sleep(5)
                self.currentToken = await ctx.cred.get_token(scope)

            log.info(f"Resuming {objName} enumeration...")


@dataclass
class AADObject:
    resource = str
    ctx: CollectorContext

    def __post_init__(self):
        self.base_url = self.ctx.cloud.endpoints.microsoft_graph_resource_id
        self._token_event = _TokenEvent(
            self.ctx, self.base_url, self.__class__.__name__
        )

    async def parse(self, value: Any) -> Any:
        return value

    async def expand(self, resource_id: str, prop: str) -> Dict[str, Any]:
        user_url = (
            f"{self.base_url}{self.ctx.tenant_id}/{self.resource}/{resource_id}/{prop}"
        )
        headers = {"Authorization": f"Bearer {self._token_event.currentToken.token}"}
        async with self.session.get(user_url, headers=headers) as expanded:
            return await expanded.json()

    async def query_objects(self, object_id: str = None):

        # Prevent logging for each backfill item
        if not object_id:
            log.info(f"Starting query for {self.__class__.__name__}")

        self.session = aiohttp.ClientSession()
        if object_id:
            user_url = (
                f"{self.base_url}{self.ctx.tenant_id}/{self.resource}/{object_id}"
            )
        else:
            user_url = f"{self.base_url}{self.ctx.tenant_id}/{self.resource}"

        next_link = True
        while next_link:

            await self._token_event.wait()
            headers = {
                "Authorization": f"Bearer {self._token_event.currentToken.token}"
            }

            async with self.session.get(user_url, headers=headers) as resp:
                response = await resp.json()
                if "odata.error" in response:
                    raise Exception(response)

                # If response contains value, it's normal enumeration...
                if response.get("value"):
                    for value in response["value"]:
                        parsedVal = await self.parse(value)
                        await sqlite_writer(
                            self.ctx.output_dir / f"{self.__class__.__name__}.sqlite",
                            parsedVal,
                        )
                        self.ctx._aad_results.update([self.__class__.__name__])
                    if "odata.nextLink" in response:
                        user_url = f"{self.base_url}/{self.tenant_id}/{response['odata.nextLink']}"
                    else:
                        next_link = False
                # ... or else it's backfill
                else:
                    parsedVal = await self.parse(response)
                    await sqlite_writer(
                        self.ctx.output_dir / f"{self.__class__.__name__}.sqlite",
                        parsedVal,
                    )
                    self.ctx._aad_results.update([self.__class__.__name__])

                    next_link = False

        # Finish cleanly
        await self.session.close()
        self._token_event.token_refresh_task.cancel()

        # Prevent logging for each backfill item
        if not object_id:
            log.info(f"Finished query for {self.__class__.__name__}")


@dataclass
class AADUser(AADObject):
    resource: str = "users"


@dataclass
class AADServicePrincipal(AADObject):
    resource = "servicePrincipals"

    async def parse(self, value):

        if not value.get("microsoftFirstParty"):
            owners = await self.expand(
                value.get("objectId") or value.get("id"), "owners"
            )
            owner_ids = [
                owner.get("objectId") or owner.get("id") for owner in owners["value"]
            ]

            value["owners"] = owner_ids
        else:
            value["owners"] = []
        return value


@dataclass
class AADApplication(AADObject):
    resource = "applications"

    async def parse(self, value):
        owners = await self.expand(value.get("objectId") or value.get("id"), "owners")
        owners_ids = []

        if owners["value"]:
            owners_ids = [
                owner.get("objectId") or owner.get("id") for owner in owners["value"]
            ]

        value["owners"] = owners_ids
        return value


@dataclass
class AADRole(AADObject):
    resource = "directoryRoles"

    async def parse(self, value):
        members = await self.expand(value.get("objectId"), "members")
        member_ids = []

        if members.get("value"):
            member_ids = [
                member.get("objectId") or member.get("id")
                for member in members["value"]
            ]
        value["members"] = member_ids

        return value


@dataclass
class AADGroup(AADObject):
    resource: str = "groups"

    async def parse(self, value):
        members = await self.expand(value.get("objectId") or value.get("id"), "members")
        member_ids = []
        if members.get("value"):
            member_ids = [
                member.get("objectId") or member.get("id")
                for member in members["value"]
            ]

        owners = await self.expand(value.get("objectId") or value.get("id"), "owners")
        owners_ids = []

        if owners.get("value"):
            owners_ids = [
                owner.get("objectId") or owner.get("id") for owner in owners["value"]
            ]

        value["members"] = member_ids
        value["owners"] = owners_ids
        return value


async def query_aad(ctx: CollectorContext, backfills: dict = None):
    """Start query for AAD objects using MS Graph endpoints"""

    GRAPH_URL = ctx.cloud.endpoints.microsoft_graph_resource_id

    log.info(f"Checking access for Microsoft Graph: {GRAPH_URL}")
    token = await ctx.cred.get_token(f"{GRAPH_URL}/.default")
    headers = {"Authorization": f"Bearer {token.token}"}

    async with aiohttp.ClientSession(headers=headers) as session:
        users_url = f"{GRAPH_URL}/beta/users"

        # Get all the AAD types to enumerate
        aad_types = AADObject.__subclasses__()

        async with session.get(users_url) as resp:
            response = await resp.json()

            # If odata.error is in response, no access to MS Graph. Abort AAD enumeration.
            if "odata.error" in response:
                log.error(
                    f"{GRAPH_URL} - {response['odata.error']['code']} - {response['odata.error']['message']['value']}"
                )
                return await session.close()

            # If in backfill mode, we only need to query for objects with RBAC permissions
            # Otherwise, do complete enumeration
            if backfills:
                await asyncio.gather(
                    list(
                        chain(
                            [
                                AADUser(ctx=ctx).query_objects(obj)
                                for obj in backfills["User"]
                            ],
                            [
                                AADGroup(ctx=ctx).query_objects(obj)
                                for obj in backfills["Group"]
                            ],
                            [
                                AADServicePrincipal(ctx=ctx).query_objects(obj)
                                for obj in backfills["ServicePrincipal"]
                            ],
                        )
                    )
                )
            else:
                await asyncio.gather(
                    *[aad_type(ctx).query_objects() for aad_type in aad_types]
                )
