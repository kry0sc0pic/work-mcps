"""Save credentials from a terminal prompt, so the password never passes through a chat.

uv run mcp-workday-login <careers-site-url> [username]
"""

import getpass
import sys

from ats_core import Credential, CredentialStoreError

from mcp_workday.accounts import WorkdayAccount
from mcp_workday.config import WorkdaySettings
from mcp_workday.server import credential_store


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    acct = WorkdayAccount.from_url(sys.argv[1])
    username = sys.argv[2] if len(sys.argv) > 2 else input("Username/email: ")
    password = getpass.getpass(f"Password for {acct.key}: ")
    try:
        credential_store(WorkdaySettings()).save(
            acct.key, Credential(username=username, password=password), meta=acct.meta("typed")
        )
    except CredentialStoreError as e:
        sys.exit(str(e))
    print(f"Saved credentials for {acct.key} ({username}) to the OS keychain.")
