#!/usr/bin/env python3
"""Import an explicitly authorized paper and PDF through Zotero's local Connector.

Usage:
  python3 import_zotero.py --metadata item.json --pdf paper.pdf \
      --collection-key ABCD1234 --out zotero.json --apply

item.json contains a Zotero conferencePaper/journalArticle/etc. JSON object and a
separate pdfURL string. Only Connector endpoints perform library writes. No API
key is requested, read, logged, or stored. Without --apply this is a read-only
preflight. Existing exact matches are inspected but never changed; incomplete
existing entries are reported for deliberate handling rather than duplicated.
"""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid


BASE = "http://127.0.0.1:23119"
PREFIX = "/api/users/0"


def normalized(text):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def canonical_url(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc.lower(), parts.path.rstrip("/"))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Client:
    def __init__(self):
        # Local calls must never pass through an environment HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.server_id = None
        self.connector_version = None

    def request(self, path, *, method="GET", data=None, headers=None, timeout=60):
        hdr = {"Zotero-API-Version": "3"}
        if self.server_id and path.startswith("/api/"):
            hdr["Zotero-Server-ID"] = self.server_id
        if self.connector_version and path.startswith("/connector/"):
            hdr["X-Zotero-Connector-API-Version"] = self.connector_version
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdr["Content-Type"] = "application/json"
        if headers:
            hdr.update(headers)
        request = urllib.request.Request(BASE + path, data=data, method=method, headers=hdr)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read()
                status, response_headers = response.status, response.headers
        except urllib.error.HTTPError as error:
            detail = error.read(2000).decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error
        if "application/json" in response_headers.get("Content-Type", "") and body:
            body = json.loads(body)
        else:
            body = body.decode("utf-8", "replace")
        return status, response_headers, body

    def get(self, path):
        return self.request(path)[2]

    def post(self, path, data):
        return self.request(path, method="POST", data=data)

    def connect(self):
        _, headers, _ = self.request("/api/")
        self.server_id = headers.get("Zotero-Server-ID")
        self.connector_version = headers.get("X-Zotero-Connector-API-Version")
        if not self.server_id or not self.connector_version:
            raise RuntimeError("Zotero must expose its server ID and Connector API version")
        return headers.get("X-Zotero-Version")


def find_target(client, collection_key):
    collection = client.get(f"{PREFIX}/collections/{collection_key}")
    all_collections = client.get(f"{PREFIX}/collections")
    by_key = {entry["key"]: entry["data"] for entry in all_collections}
    names = []
    cursor = collection["data"]
    while cursor:
        names.insert(0, cursor["name"])
        cursor = by_key.get(cursor.get("parentCollection"))
    _, _, selection = client.post("/connector/getSelectedCollection", {})
    tree_path, matches, library_target = [], [], None
    for target in selection["targets"]:
        level = target["level"]
        if level == 0:
            library_target = target
            tree_path = []
            continue
        tree_path = tree_path[:level - 1] + [target["name"]]
        if (library_target["name"] == collection["library"]["name"]
                and tree_path == names):
            matches.append((target, library_target))
    if len(matches) != 1:
        raise RuntimeError("Cannot unambiguously map collection key to Connector target")
    target, library = matches[0]
    if library["id"] != "L1":
        raise RuntimeError("This importer only supports the local user's personal library")
    if not target.get("filesEditable"):
        raise RuntimeError("Destination does not allow file attachments")
    return collection, target, selection


def exact_matches(client, metadata):
    # Fetch the top-level personal-library inventory once to avoid search token
    # differences (punctuation, normalization) allowing duplicate creation.
    entries = client.get(f"{PREFIX}/items/top")
    wanted_title = normalized(metadata["title"])
    wanted_url = canonical_url(metadata["url"])
    wanted_doi = metadata.get("DOI", "").strip().casefold()
    matches = []
    for entry in entries:
        data = entry["data"]
        if data.get("itemType") in ("attachment", "note"):
            continue
        same_title = normalized(data.get("title", "")) == wanted_title
        same_url = bool(data.get("url")) and canonical_url(data["url"]) == wanted_url
        same_doi = wanted_doi and data.get("DOI", "").strip().casefold() == wanted_doi
        if same_title or same_url or same_doi:
            matches.append(entry)
    return matches


def inspect_entry(client, entry, collection_key, pdf_digest):
    key = entry["key"]
    children = client.get(f"{PREFIX}/items/{key}/children")
    attachments = []
    for child in children:
        data = child["data"]
        if data.get("itemType") != "attachment":
            continue
        info = {"key": child["key"], "title": data.get("title"),
                "content_type": data.get("contentType"), "url": data.get("url"),
                "link_mode": data.get("linkMode")}
        if data.get("contentType") == "application/pdf" and data.get("linkMode") != "linked_url":
            try:
                file_url = client.get(f"{PREFIX}/items/{child['key']}/file/view/url")
                parsed = urllib.parse.urlsplit(file_url)
                if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                    raise ValueError("Expected a local file URL")
                stored = Path(urllib.request.url2pathname(parsed.path))
                info["storage_file"] = str(stored)
                info["exists"] = stored.is_file()
                if stored.is_file():
                    info["bytes"] = stored.stat().st_size
                    info["sha256"] = sha256_file(stored)
                    with stored.open("rb") as stream:
                        info["pdf_header_valid"] = stream.read(5) == b"%PDF-"
                    info["matches_source_pdf"] = info["sha256"] == pdf_digest
            except (RuntimeError, OSError, ValueError) as error:
                info["verification_error"] = str(error)
        attachments.append(info)
    collection_items = client.get(f"{PREFIX}/collections/{collection_key}/items/top")
    in_collection = any(item["key"] == key for item in collection_items)
    matching_pdf = any(a.get("matches_source_pdf") and a.get("pdf_header_valid") for a in attachments)
    return {"item_key": key, "item_type": entry["data"]["itemType"],
            "title": entry["data"].get("title"), "item_url": entry["data"].get("url"),
            "item_data": entry["data"], "in_target_collection": in_collection,
            "attachments": attachments, "verified_pdf": matching_pdf,
            "zotero_select_uri": f"zotero://select/library/items/{key}",
            "complete": in_collection and matching_pdf}


def write_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--collection-key", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "collection_key": args.collection_key, "status": "preflight"}
    try:
        metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        pdf_url = metadata.pop("pdfURL")
        if not metadata.get("title") or not metadata.get("itemType") or not metadata.get("url"):
            raise ValueError("metadata requires title, itemType, url, and pdfURL")
        if metadata["itemType"] in ("attachment", "note", "webpage"):
            raise ValueError("Use a bibliographic paper item, not a webpage or attachment")
        pdf_bytes = args.pdf.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("Input file does not have a PDF header")
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        result.update({"source_pdf": str(args.pdf.resolve()), "source_pdf_url": pdf_url,
                       "source_pdf_bytes": len(pdf_bytes), "source_pdf_sha256": digest})
        client = Client()
        result["zotero_version"] = client.connect()
        result["server_id"] = client.server_id
        collection, target, selection = find_target(client, args.collection_key)
        result.update({"collection_name": collection["data"]["name"],
                       "connector_target": target["id"],
                       "collection_uri": f"zotero://select/library/collections/{args.collection_key}"})
        matches = exact_matches(client, metadata)
        if matches:
            result["status"] = "existing"
            result["matches"] = [inspect_entry(client, entry, args.collection_key, digest) for entry in matches]
            result["complete"] = len(matches) == 1 and result["matches"][0]["complete"]
            if len(matches) == 1:
                result.update(result["matches"][0])
            result["note"] = "Existing matches were inspected without modifying or duplicating them."
        elif not args.apply:
            result.update({"complete": False, "ready_to_import": True})
        else:
            if selection["libraryID"] != 1 or not selection.get("libraryEditable"):
                raise RuntimeError("Select the destination personal library in Zotero before importing")
            # Validate field names before the first mutation.
            fields = client.get("/api/itemTypeFields?" + urllib.parse.urlencode({"itemType": metadata["itemType"]}))
            allowed = {field["field"] for field in fields} | {"itemType", "creators", "tags", "notes", "attachments", "seeAlso"}
            unknown = set(metadata) - allowed
            if unknown:
                raise ValueError("Unsupported metadata fields: " + ", ".join(sorted(unknown)))
            session = str(uuid.uuid4())
            connector_item_id = "paper-" + uuid.uuid4().hex
            result.update({"session_id": session, "connector_item_id": connector_item_id,
                           "status": "import_started", "complete": False})
            write_result(args.out, result)
            item = dict(metadata, id=connector_item_id)
            status, _, _ = client.post("/connector/saveItems", {
                "sessionID": session, "uri": metadata["url"], "items": [item]})
            if status != 201:
                raise RuntimeError(f"saveItems returned unexpected status {status}")
            result["status"] = "item_created"
            result["save_items_http_status"] = status
            write_result(args.out, result)
            status, _, _ = client.post("/connector/updateSession", {
                "sessionID": session, "target": target["id"], "tags": []})
            if status != 200:
                raise RuntimeError(f"updateSession returned unexpected status {status}")
            result["update_session_http_status"] = status
            metadata_header = json.dumps({"parentItemID": connector_item_id,
                                          "title": "Full Text PDF", "url": pdf_url}, ensure_ascii=True)
            status, _, _ = client.request(
                "/connector/saveAttachment?" + urllib.parse.urlencode({"sessionID": session}),
                method="POST", data=pdf_bytes, headers={"Content-Type": "application/pdf",
                    "Content-Length": str(len(pdf_bytes)), "X-Metadata": metadata_header}, timeout=60)
            if status != 201:
                raise RuntimeError(f"saveAttachment returned unexpected status {status}")
            result["save_attachment_http_status"] = status
            result["status"] = "attachment_created"
            write_result(args.out, result)
            matches = exact_matches(client, metadata)
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one matching paper after import")
            result.update(inspect_entry(client, matches[0], args.collection_key, digest))
            result["status"] = "imported" if result["complete"] else "verification_incomplete"
        write_result(args.out, result)
        print(json.dumps({key: result.get(key) for key in
                          ("status", "complete", "item_key", "collection_key", "connector_target", "verified_pdf")},
                         ensure_ascii=False))
        return 0 if result.get("complete") or result.get("ready_to_import") else 2
    except Exception as error:
        result["error"] = str(error)
        result["complete"] = False
        write_result(args.out, result)
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
