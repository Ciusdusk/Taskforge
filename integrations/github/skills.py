"""Complete native Git tree snapshots; never infer deletion from partial trees."""

from typing import Literal
from urllib.parse import quote

from pydantic import JsonValue

from integrations.github.errors import CollectionError
from integrations.github.http import GitHubHTTP
from integrations.github.models import SkillChange, SkillSnapshot
from integrations.github.remote import Tree, parse


async def collect_skills(
    http: GitHubHTTP,
    repository: str,
    repository_id: int,
    target: str,
    tree_sha: str,
    previous: SkillSnapshot | None,
) -> tuple[SkillSnapshot, tuple[SkillChange, ...], dict[str, JsonValue]]:
    root = f"/repos/{repository}/git/trees/"
    recursive = parse(Tree, (await http.get(root + tree_sha + "?recursive=1")).body)
    if recursive.sha != tree_sha:
        raise CollectionError("Skill tree identity does not match the fixed commit")
    files: dict[str, str] = {}
    symlinks = 0
    submodules = 0
    seen_paths: set[str] = set()

    def include(tree: Tree, prefix: str, *, recursive: bool) -> list[tuple[str, str]]:
        nonlocal symlinks, submodules
        children = []
        for entry in tree.tree:
            if not recursive and "/" in entry.path:
                raise CollectionError("Nonrecursive tree contains a nested path")
            path = prefix + entry.path
            if path in seen_paths:
                raise CollectionError("Duplicate Git tree path")
            seen_paths.add(path)
            expected_type = {"040000": "tree", "160000": "commit"}.get(entry.mode, "blob")
            if entry.type != expected_type:
                raise CollectionError("Git tree mode and type disagree")
            if entry.mode == "120000":
                symlinks += 1
            elif entry.mode == "160000":
                submodules += 1
            elif entry.type == "tree":
                children.append((path + "/", entry.sha))
            elif path.rsplit("/", 1)[-1] == "SKILL.md":
                files[path] = entry.sha
        return children

    if not recursive.truncated:
        modes = {entry.path: entry.mode for entry in recursive.tree}
        for path in modes:
            parts = path.split("/")
            if any(
                modes.get("/".join(parts[:index])) != "040000" for index in range(1, len(parts))
            ):
                raise CollectionError("Recursive tree has missing or non-tree ancestors")
        include(recursive, "", recursive=True)
        method = "recursive"
    else:
        # Discard the partial result, including any apparent missing definitions.
        pending = [("", tree_sha, frozenset[str]())]
        while pending:
            prefix, sha, ancestors = pending.pop()
            if sha in ancestors:
                raise CollectionError("Git tree ancestry cycle")
            tree = parse(Tree, (await http.get(root + sha)).body)
            if tree.sha != sha or tree.truncated:
                raise CollectionError("Subtree is incomplete or has the wrong identity")
            for child_prefix, child_sha in include(tree, prefix, recursive=False):
                pending.append((child_prefix, child_sha, ancestors | {sha}))
        method = "subtrees"

    snapshot = SkillSnapshot(repository_id=repository_id, commit_sha=target, files=files)
    if previous is not None and previous.commit_sha == target and previous.files != files:
        raise CollectionError("Immutable skill snapshot changed at the same commit")
    before = {} if previous is None else previous.files
    changes = []
    links: list[JsonValue] = []
    for path in sorted(before.keys() | files.keys()):
        if before.get(path) == files.get(path):
            continue
        kind: Literal["added", "modified", "deleted"] = (
            "deleted" if path not in files else "added" if path not in before else "modified"
        )
        before_sha = "" if previous is None else previous.commit_sha
        changes.append(
            SkillChange(
                repository_id=repository_id,
                before_sha=before_sha,
                after_sha=target,
                path=path,
                kind=kind,
                blob_sha=files.get(path),
            )
        )
        base_url = f"https://github.com/{repository}/blob/"
        links.append(
            {
                "path": path,
                "kind": kind,
                "initial": previous is None,
                "before_url": base_url + before_sha + "/" + quote(path, safe="/")
                if path in before
                else None,
                "after_url": base_url + target + "/" + quote(path, safe="/")
                if path in files
                else None,
            }
        )
    coverage: dict[str, JsonValue] = {
        "complete": True,
        "method": method,
        "tree_sha": tree_sha,
        "files": len(files),
        "scope": "ordinary SKILL.md in the fixed commit's native tree; definitions only",
        "excluded": {"symlinks": symlinks, "submodules": submodules},
        "changes": links,
    }
    return snapshot, tuple(changes), coverage
