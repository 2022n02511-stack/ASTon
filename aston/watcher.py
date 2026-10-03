from aston.ingestion import get_latest_pypi_version
from pathlib import Path
import re

def watcher(path : str) -> dict:

    requirements = []

    folder_path = Path(path)

    if not folder_path.exists():
        print("Path doesn't exist.")

    for file in folder_path.iterdir():

        if file.name == 'requirements.txt':


            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.strip()    
                # print(line)

                if not line or line.startswith("#") or line.startswith("-"):
                    continue

                match = re.match(r"^([a-zA-Z0-9\-_]+)\s*(?:[><=!]=?~?=?\s*([0-9\.]+.*))?", line)
                if match:
                    # print(match)
                    pkg_name, current_version = match.groups()
                    target_version = get_latest_pypi_version(pkg_name.lower())
                    requirements.append({
                        "pkg_name" : pkg_name.lower(),
                        "current_version" : current_version.strip() if current_version else None,
                        "target_version" : target_version if target_version else None
                        # pkg_name.lower() : {
                        #     current_version.strip() if current_version else None,
                        #     target_version if target_version else None
                        # }
                    })
                    # print(requirements)


    if requirements is None:
        print("Nothing found.")
    else:
        return requirements    
