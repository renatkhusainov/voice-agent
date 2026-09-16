import os
import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel

load_dotenv(override=True)


class CallInfo(BaseModel):
    """Caller details fetched from the Twilio REST API."""

    from_number: str | None = None
    to_number: str | None = None


async def get_call_info(call_sid: str | None) -> CallInfo | None:
    """Fetch call information from the Twilio REST API using aiohttp.

    Args:
        call_sid: The Twilio call SID (e.g. call_data.call_id), or None.

    Returns:
        A CallInfo with the caller's numbers, or None if it couldn't be fetched.
    """
    if not call_sid:
        return None

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return None

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        # Use HTTP Basic Auth with aiohttp
        auth = aiohttp.BasicAuth(account_sid, auth_token)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return None

                data = await response.json()

                return CallInfo(from_number=data.get("from"), to_number=data.get("to"))

    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return None