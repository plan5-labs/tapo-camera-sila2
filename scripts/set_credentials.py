"""Store Tapo camera credentials in the OS credential store (run this yourself, interactively).

    uv run scripts/set_credentials.py              # profile "default"
    uv run scripts/set_credentials.py --profile cam2
    uv run scripts/set_credentials.py --delete

The password is read with getpass (not echoed) and is never printed.
"""

import argparse
import getpass

from tapo_camera_controller import credentials


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    if args.delete:
        credentials.delete(args.profile)
        print(f"deleted profile {args.profile!r}")
        return

    host = input("Camera IP address: ").strip()
    user = input("Camera account user name: ").strip()
    pw = getpass.getpass("Camera account password: ")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("passwords do not match")
    credentials.save(credentials.Credentials(host, user, pw), args.profile)
    print(f"saved profile {args.profile!r} to the OS credential store (service {credentials.SERVICE!r})")


if __name__ == "__main__":
    main()
