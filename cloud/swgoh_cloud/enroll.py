"""Create a cloud account/source and save credentials locally without printing them."""
import argparse
import json
import os
from pathlib import Path
import httpx
from .bridge import validate_cloud_url


def private_json(path, value):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,"w") as stream:
        json.dump(value,stream,indent=2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True);parser.add_argument("--alias",default="main")
    parser.add_argument("--source",default="home-pc");parser.add_argument("--directory",default="cloud/.state")
    parser.add_argument("--inspector-url-file",default="/home/korgulos/Games/ea-app/drive_c/Program Files/EA Games/SWGoH/BepInEx/external-inspector-url.txt")
    args=parser.parse_args();url=validate_cloud_url(args.url)
    admin=os.environ.get("CLOUD_ADMIN_TOKEN","")
    if len(admin)<32:
        raise SystemExit("Set CLOUD_ADMIN_TOKEN in your environment for initial enrollment")
    directory=Path(args.directory).resolve();directory.mkdir(parents=True,exist_ok=True);os.chmod(directory,0o700)
    if (directory/"reader.local.json").exists() or (directory/"bridge.local.json").exists():
        raise SystemExit("Local enrollment files already exist; reuse them instead of creating duplicate credentials")
    with httpx.Client(timeout=60,follow_redirects=False,trust_env=False,headers={"Authorization":"Bearer "+admin}) as client:
        response=client.post(url+"/v1/accounts",json={"alias":args.alias});response.raise_for_status();account=response.json()
        # Preserve the one-time read token even if the next network request fails.
        private_json(directory/"reader.local.json",dict(cloud_url=url,account_id=account["account_id"],read_token=account["read_token"]))
        response=client.post(url+f'/v1/accounts/{account["account_id"]}/sources',json={"name":args.source});response.raise_for_status();source=response.json()
        private_json(directory/"bridge.local.json",dict(cloud_url=url,ingest_token=source["ingest_token"],
            inspector_url_file=args.inspector_url_file,outbox_path=str(directory/"outbox.sqlite3"),interval_seconds=5))
    print("Enrollment saved in private local files. No credentials printed.")

if __name__ == "__main__":
    main()
