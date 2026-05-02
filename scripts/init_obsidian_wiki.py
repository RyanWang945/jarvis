from pathlib import Path

from app.obsidian_wiki import ObsidianWikiService


def main() -> None:
    workspace_path = Path(r"E:\pythonProject\jarvis\data\obsidian_wiki")
    service = ObsidianWikiService(workspace_path)
    service.init_workspace()
    print(service.vault_path)


if __name__ == "__main__":
    main()
