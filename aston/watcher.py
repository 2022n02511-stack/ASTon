from aston.ingestion import get_latest_pypi_version
from pathlib import Path
from packaging.version import Version
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

                cur_ver_bump, tar_ver_bump = None
                # print(line)

                if not line or line.startswith("#") or line.startswith("-"):
                    continue

                match = re.match(r"^([a-zA-Z0-9\-_]+)\s*(?:[><=!]=?~?=?\s*([0-9\.]+.*))?", line)
                if match:
                    # print(match)
                    pkg_name, current_version = match.groups()
                    if current_version:
                        cur_ver_bump = Version(current_version.strip())    
                    target_version = get_latest_pypi_version(pkg_name.lower())
                    if target_version:
                        tar_ver_bump = Version(target_version)

                    if tar_ver_bump > cur_ver_bump:


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
        print("Nothing found, everything is upto date")
    else:
        return requirements    
