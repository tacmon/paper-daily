#!/usr/bin/env python3
"""Small, offline, standard-library daily paper archive. See WORKFLOW.md."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo
import zipfile

TIMEZONE = ZoneInfo("Asia/Shanghai")
GENERATED = "<!-- paper-library:generated -->"
ARTIFACTS = {
    "pdf": "paper.pdf", "citation": "citation.bib", "reading": "reading.md",
    "script": "script.md", "slides": "slides.pptx", "video": "video.mp4",
    "subtitles": "subtitles.srt", "notes": "notes.md", "notes_html": "notes.html",
}
CHECKS = ("full_text_read", "slides_rendered", "video_audio_checked",
          "video_subtitles_checked", "video_duration_checked")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,119}\Z")


def today(now=None):
    """An aware injected time makes this deterministic in tests."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("time must include a timezone")
    return now.astimezone(TIMEZONE).date().isoformat()


def timestamp():
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def valid_day(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def canonical_id(kind, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind}: identifier must be a nonempty string")
    value = unquote(value.strip())
    if kind == "doi":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
        value = value.lower()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            raise ValueError(f"invalid DOI: {value}")
    elif kind == "arxiv":
        value = re.sub(r"^(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", value, flags=re.I)
        value = re.sub(r"\.pdf$", "", value, flags=re.I)
        value = re.sub(r"v\d+$", "", value, flags=re.I).lower()
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*(?:\.[a-z]{2})?/\d{7})", value):
            raise ValueError(f"invalid arXiv ID: {value}")
    elif kind == "pmlr":
        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            if parsed.hostname != "proceedings.mlr.press":
                raise ValueError("PMLR URL must be on proceedings.mlr.press")
            value = parsed.path
        value = value.strip("/").lower()
        value = re.sub(r"\.(?:html|pdf)$", "", value)
        pieces = value.split("/")
        if len(pieces) == 3 and pieces[1] == pieces[2]:
            pieces.pop()  # v80/florensa18a/florensa18a.pdf
        value = "/".join(pieces)
        if not re.fullmatch(r"v\d+/[a-z][a-z0-9_-]*", value):
            raise ValueError(f"PMLR needs volume and paper ID (for example v80/florensa18a): {value}")
    else:
        raise ValueError(f"unsupported identifier kind: {kind}")
    return value


def identities(metadata):
    values = metadata.get("identifiers", {})
    if not isinstance(values, dict):
        raise ValueError("identifiers must be an object")
    return {(kind, canonical_id(kind, value)) for kind, value in values.items() if value}


def stable_id(ids):
    for kind in ("arxiv", "doi", "pmlr"):
        matches = [value for key, value in ids if key == kind]
        if matches:
            value = matches[0]
            if kind == "doi":
                return "doi-" + hashlib.sha256(value.encode()).hexdigest()[:16]
            return kind + "-" + value.replace("/", "-")
    raise ValueError("provide at least one DOI, arXiv or PMLR identifier")


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def inside(base, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("artifact path must be a nonempty relative path")
    result = base / relative
    if not result.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"path escapes its paper directory: {relative}")
    return result


def load_library(root):
    papers, days, errors = {}, {}, []
    for section, filename in (("papers", "metadata.json"), ("days", "paper.json")):
        for directory in sorted((root / section).glob("*")):
            if directory.is_dir() and not (directory / filename).is_file():
                errors.append(f"{directory.relative_to(root)}/{filename}: missing source record")
    for path in sorted((root / "papers").glob("*/metadata.json")):
        try:
            if not ID_RE.fullmatch(path.parent.name):
                raise ValueError("unsafe stable ID")
            data = read_json(path)
            if data.get("id") != path.parent.name:
                raise ValueError("metadata.id must equal its paper directory name")
            identities(data)
            papers[data["id"]] = data
        except (OSError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
    for path in sorted((root / "days").glob("*/paper.json")):
        try:
            data = read_json(path)
            day = valid_day(path.parent.name)
            if data.get("date") != day or data.get("timezone") != "Asia/Shanghai":
                raise ValueError("date must match its directory; timezone must be Asia/Shanghai")
            if not isinstance(data.get("paper_id"), str) or data["paper_id"] not in papers:
                raise ValueError("paper_id does not resolve to valid archived metadata")
            days[day] = data
        except (OSError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
    return papers, days, errors


def duplicates(papers):
    owners, errors = {}, []
    for paper_id, data in papers.items():
        for identity in identities(data):
            if identity in owners:
                errors.append(f"duplicate {identity[0]}:{identity[1]}: {owners[identity]}, {paper_id}")
            else:
                owners[identity] = paper_id
    return errors


def readiness(root, data):
    problems = []
    paths = data.get("artifacts", {})
    if not isinstance(paths, dict):
        return ["artifacts must be an object"]
    base = root / "papers" / data["id"]
    for key in ARTIFACTS:
        try:
            path = inside(base, paths.get(key))
            if not path.is_file() or path.stat().st_size == 0:
                problems.append(f"missing/empty artifact: {key}")
                continue
            if key == "pdf":
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        problems.append("pdf does not have a PDF header")
            if key == "slides":
                try:
                    with zipfile.ZipFile(path) as deck:
                        if "ppt/presentation.xml" not in deck.namelist():
                            problems.append("slides is not an editable PPTX")
                except (OSError, zipfile.BadZipFile):
                    problems.append("slides is not an editable PPTX")
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"artifact {key}: {exc}")
    checks = data.get("verification", {})
    if not isinstance(checks, dict):
        checks = {}
    for key in CHECKS:
        if checks.get(key) is not True:
            problems.append(f"verification pending: {key}")
    duration = checks.get("video_duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 600 <= duration <= 900:
        problems.append("video_duration_seconds must be between 600 and 900")
    return problems


def effective_status(root, data):
    return "ready" if data.get("status") == "ready" and not readiness(root, data) else "partial"


def author_name(author):
    if isinstance(author, str):
        return author.strip()
    return author.get("name", "").strip() if isinstance(author, dict) and isinstance(author.get("name"), str) else ""


def author_key(name):
    normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode()).strip("-")[:60]
    return (slug or "author") + "-" + hashlib.sha256(normalized.encode()).hexdigest()[:8]


def verified_relation(relation):
    if not isinstance(relation, dict):
        return False
    evidence = relation.get("evidence")
    return (relation.get("relation") == "cites" and relation.get("verified") is True
            and isinstance(evidence, dict)
            and all(isinstance(evidence.get(key), str) and evidence[key].strip()
                    for key in ("source", "locator")))


def expected_links(papers):
    links = {}
    for paper_id, data in papers.items():
        authors = data.get("authors", [])
        for author in authors if isinstance(authors, list) else []:
            name = author_name(author)
            if name:
                links[f"indexes/authors/{author_key(name)}/{paper_id}"] = f"papers/{paper_id}"
        citations = data.get("citations", [])
        for relation in citations if isinstance(citations, list) else []:
            if (verified_relation(relation) and isinstance(relation.get("target_id"), str)
                    and relation["target_id"] in papers and relation["target_id"] != paper_id):
                links[f"indexes/citations/{paper_id}/{relation['target_id']}"] = f"papers/{relation['target_id']}"
    return links


def generated_write(path, content):
    if path.exists() and (path.is_symlink() or not path.is_file() or GENERATED not in path.read_text(encoding="utf-8")[:200]):
        raise ValueError(f"refusing to overwrite a user file: {path}")
    atomic_text(path, GENERATED + "\n" + content)


def rebuild(root):
    papers, days, errors = load_library(root)
    errors += duplicates(papers)
    if errors:
        raise ValueError("fix metadata before rebuilding:\n" + "\n".join(errors))
    for section in ("authors", "citations"):
        (root / "indexes" / section).mkdir(parents=True, exist_ok=True)
    manifest_path = root / "indexes" / "generated_links.json"
    old = read_json(manifest_path) if manifest_path.exists() else {}
    if old and old.get("generator") != "paper_library.py":
        raise ValueError("refusing to replace an unrecognized generated_links.json")
    old_links = old.get("links", {})
    if not isinstance(old_links, dict):
        raise ValueError("invalid generated link manifest")
    desired, owned = expected_links(papers), {}
    warnings = []
    for relative, target in old_links.items():
        if relative in desired:
            continue
        path = inside(root, relative)
        if not relative.startswith("indexes/"):
            raise ValueError("generated link manifest includes a path outside indexes")
        # Delete only a link created by this program whose exact target is unchanged.
        if path.is_symlink() and os.readlink(path) == target:
            path.unlink()
    for relative, target in desired.items():
        path = root / relative
        actual_target = os.path.relpath(root / target, path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            if os.readlink(path) == actual_target:
                if old_links.get(relative) == actual_target:
                    owned[relative] = actual_target
                continue
            if old_links.get(relative) == os.readlink(path):
                path.unlink()
            else:
                warnings.append(f"preserved conflicting user symlink: {relative}")
                continue
        elif path.exists():
            warnings.append(f"preserved conflicting user file: {relative}")
            continue
        path.symlink_to(actual_target, target_is_directory=True)
        owned[relative] = actual_target
    atomic_json(manifest_path, {"generator": "paper_library.py", "links": owned})
    markdown = ["# 每日论文索引", "", "北京时间（Asia/Shanghai）。状态依实际文件与人工核验记录计算。", "",
                "| 日期 | 论文 | 状态 |", "| --- | --- | --- |"]
    rows = []
    for day, record in sorted(days.items(), reverse=True):
        data = papers[record["paper_id"]]
        paper_id, title = data["id"], str(data.get("title", data["id"]))
        status = effective_status(root, data)
        label = title.replace("|", " / ").replace("\n", " ").replace("[", "(").replace("]", ")")
        markdown.append(f"| {day} | [{label}](../papers/{paper_id}/metadata.json) | {status} |")
        artifacts = data.get("artifacts", {})
        artifact_links = []
        for key, path in artifacts.items() if isinstance(artifacts, dict) else []:
            try:
                if inside(root / "papers" / paper_id, path).is_file():
                    url = "papers/" + quote(paper_id) + "/" + quote(path, safe="/")
                    artifact_links.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(key)}</a>')
            except (ValueError, TypeError):
                pass
        rows.append(f'<tr><td>{day}</td><td><a href="papers/{paper_id}/metadata.json">{html.escape(title)}</a><br><span>{" · ".join(artifact_links)}</span></td><td>{status}</td></tr>')
    markdown += ["", "[作者索引](authors/) · [已核验引用索引](citations/)", "", "## 已归档论文", ""]
    for paper_id, data in sorted(papers.items()):
        title = str(data.get("title", paper_id)).replace("\n", " ").replace("[", "(").replace("]", ")")
        markdown.append(f"- [{title}](../papers/{paper_id}/metadata.json) — {effective_status(root, data)}")
    generated_write(root / "indexes" / "README.md", "\n".join(markdown) + "\n")
    page = '''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>论文日报</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:3rem auto;padding:0 1rem;background:#f8fafc;color:#132339}h1{font-size:2rem}table{border-collapse:collapse;width:100%;background:white}th,td{padding:1rem;text-align:left;border-bottom:1px solid #dbe3ed}td:first-child{white-space:nowrap}a{color:#176184}span{font-size:.85rem}footer{margin-top:2rem}</style>
<h1>论文日报</h1><p>北京时间 · Asia/Shanghai · 每日一篇 · 中文视频 10–15 分钟</p>
<table><thead><tr><th>日期</th><th>论文与文件</th><th>状态</th></tr></thead><tbody>''' + "\n".join(rows) + '''</tbody></table>
<footer><a href="http://127.0.0.1:8766/reading-log">写总结与查看每日一句</a> · <a href="reading_log.md">原始记录</a> · <a href="http://127.0.0.1:8766/">选文与笔记检索工作台</a> · <a href="indexes/README.md">全部归档与作者、引用索引</a> · <a href="WORKFLOW.md">阅读流程</a> · <a href="reading_profile.md">阅读偏好</a></footer></html>'''
    generated_write(root / "index.html", page)
    if (root / '.obsidian').is_dir():
        from scripts.obsidian_bridge import refresh_index
        try:
            result = refresh_index(root, papers, days)
            if result.get('warning'):
                warnings.append(result['warning'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            warnings.append('Obsidian导航未更新，既有日报保留：' + str(exc))
    return {"papers": len(papers), "days": len(days), "links": len(owned), "warnings": warnings}


def validate(root, require_ready=False):
    papers, days, errors = load_library(root)
    errors += duplicates(papers)
    warnings = []
    if require_ready and not days:
        errors.append("no daily paper records are present")
    assigned = {}
    for day, record in days.items():
        assigned.setdefault(record["paper_id"], []).append(day)
    for paper_id, data in papers.items():
        if data.get("status") not in ("partial", "ready"):
            errors.append(f"{paper_id}: status must be partial or ready")
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            errors.append(f"{paper_id}: title is missing")
        if not identities(data):
            errors.append(f"{paper_id}: DOI/arXiv/PMLR identifier is missing")
        if not isinstance(data.get("authors"), list) or not data["authors"] or any(not author_name(a) for a in data["authors"]):
            errors.append(f"{paper_id}: authors must be a nonempty list of names or name objects")
        missing = readiness(root, data)
        bucket = errors if data.get("status") == "ready" or require_ready else warnings
        bucket.extend(f"{paper_id}: {problem}" for problem in missing)
        if require_ready and data.get("status") != "ready":
            errors.append(f"{paper_id}: status is partial; explicit acceptance is pending")
        citations = data.get("citations", [])
        if not isinstance(citations, list):
            errors.append(f"{paper_id}: citations must be a list")
            citations = []
        for relation in citations:
            if not isinstance(relation, dict):
                errors.append(f"{paper_id}: citation must be an object")
            elif relation.get("verified") is True and not verified_relation(relation):
                errors.append(f"{paper_id}: verified citation needs relation=cites and evidence.source/locator")
            elif relation.get("target_id") is not None and not isinstance(relation["target_id"], str):
                errors.append(f"{paper_id}: citation target_id must be a string or null")
            elif verified_relation(relation) and relation.get("target_id") not in papers:
                warnings.append(f"{paper_id}: cited target is not archived; no link created: {relation.get('target_id')}")
            elif relation.get("target_id") == paper_id:
                errors.append(f"{paper_id}: self-reference is not a cross-paper citation")
        if paper_id not in assigned:
            warnings.append(f"{paper_id}: archived but not assigned to a day")
        if len(assigned.get(paper_id, [])) > 1:
            warnings.append(f"{paper_id}: assigned to multiple days: {', '.join(assigned[paper_id])}")
    for relative, target in expected_links(papers).items():
        path = root / relative
        if not path.is_symlink():
            warnings.append(f"index missing; run rebuild: {relative}")
        elif os.path.isabs(os.readlink(path)):
            errors.append(f"index symlink must be relative: {relative}")
        elif path.resolve() != (root / target).resolve():
            errors.append(f"index points to wrong paper: {relative}")
    if (root / "indexes").exists():
        for path in (root / "indexes").rglob("*"):
            if path.is_symlink() and not path.exists():
                errors.append(f"broken link: {path.relative_to(root)}")
    return {"ok": not errors, "papers": len(papers), "days": len(days),
            "errors": errors, "warnings": warnings}


def register(root, args):
    valid_day(args.date)
    ids = {(kind, canonical_id(kind, getattr(args, kind))) for kind in ("doi", "arxiv", "pmlr") if getattr(args, kind)}
    if not ids:
        raise ValueError("provide --doi, --arxiv or --pmlr")
    papers, days, errors = load_library(root)
    errors += duplicates(papers)
    if errors:
        raise ValueError("fix invalid archive metadata first:\n" + "\n".join(errors))
    matches = [paper_id for paper_id, data in papers.items() if identities(data) & ids]
    if len(matches) > 1:
        raise ValueError("identifiers connect multiple records; resolve manually without deleting files: " + ", ".join(matches))
    paper_id = matches[0] if matches else args.id or stable_id(ids)
    if not ID_RE.fullmatch(paper_id):
        raise ValueError("--id must use lowercase letters, digits, dots, underscores or hyphens")
    if not matches and paper_id in papers:
        raise ValueError("stable ID collision with unrelated identifiers")
    existing_days = [day for day, data in days.items() if data["paper_id"] == paper_id and day != args.date]
    if existing_days and not args.allow_repeat:
        raise ValueError("paper already assigned on " + ", ".join(existing_days) + "; use --allow-repeat only for an intentional revisit")
    if args.date in days and days[args.date]["paper_id"] != paper_id:
        raise ValueError("day already contains another paper; existing assignment is preserved")
    metadata_path = root / "papers" / paper_id / "metadata.json"
    day_path = root / "days" / args.date / "paper.json"
    if matches:
        data = papers[paper_id]
        for kind, value in ids:
            old = data.setdefault("identifiers", {}).get(kind)
            if old and canonical_id(kind, old) != value:
                raise ValueError(f"conflicting {kind} for the existing record")
            data["identifiers"][kind] = value
        data["updated_at"] = timestamp()
    else:
        if metadata_path.exists() or metadata_path.is_symlink():
            raise ValueError("refusing to overwrite existing metadata")
        if not args.title or not args.author:
            raise ValueError("a new paper needs --title and one or more --author")
        data = {"schema_version": 1, "id": paper_id, "title": args.title,
                "authors": [{"name": name} for name in args.author], "year": args.year,
                "identifiers": dict(sorted(ids)), "status": "partial",
                "created_at": timestamp(), "updated_at": timestamp(),
                "sources": [], "artifacts": dict(ARTIFACTS),
                "verification": {**{key: False for key in CHECKS}, "video_duration_seconds": None, "notes": ""},
                "citations": [], "experiment": {"level": "not_run", "reason": "尚未评估是否值得做最小机制实验"},
                "zotero": {"collection": "Paper Daily by AI", "status": "not_attempted", "item_key": None, "reason": ""}}
    # Validate both destinations before changing either. An interrupted second write
    # leaves an unassigned paper that validate reports and register can safely reuse.
    if day_path.is_symlink():
        raise ValueError("refusing to replace a day symlink")
    atomic_json(metadata_path, data)
    if args.date not in days:
        atomic_json(day_path, {"schema_version": 1, "date": args.date, "timezone": "Asia/Shanghai", "paper_id": paper_id, "created_at": timestamp()})
    return {"paper_id": paper_id, "reused": bool(matches), "date": args.date,
            "metadata": str(metadata_path), "status": effective_status(root, data)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="archive root; defaults to script directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("today", help="print today's date in Asia/Shanghai")
    commands.add_parser("rebuild", help="regenerate HTML, Markdown and relative symlink indexes")
    validation = commands.add_parser("validate", help="offline structural and completeness checks")
    validation.add_argument("--require-ready", action="store_true")
    lookup = commands.add_parser("lookup", help="find already archived identifiers")
    registration = commands.add_parser("register", help="register or reuse a paper, and assign one day")
    for sub in (lookup, registration):
        for kind in ("doi", "arxiv", "pmlr"):
            sub.add_argument("--" + kind)
    registration.add_argument("--id", help="optional stable ID for a new record")
    registration.add_argument("--title")
    registration.add_argument("--author", action="append")
    registration.add_argument("--year", type=int)
    registration.add_argument("--date", default=today())
    registration.add_argument("--allow-repeat", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "today":
            print(today())
            return 0
        if args.command == "register":
            result = register(root, args)
        elif args.command == "rebuild":
            result = rebuild(root)
        elif args.command == "validate":
            result = validate(root, args.require_ready)
        else:
            ids = {(kind, canonical_id(kind, getattr(args, kind))) for kind in ("doi", "arxiv", "pmlr") if getattr(args, kind)}
            if not ids:
                raise ValueError("provide at least one identifier")
            papers, _, errors = load_library(root)
            if errors:
                raise ValueError("\n".join(errors))
            result = {"matches": [key for key, value in papers.items() if identities(value) & ids]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "validate" and not result["ok"] else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
