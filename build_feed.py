#!/usr/bin/env python3
"""Read Shopify + an existing Google RSS feed; create supplemental relationships.
Python 3.10+, standard library only. No Shopify writes or Google uploads.
"""
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

G = "http://base.google.com/ns/1.0"
ET.register_namespace("g", G)
ROOT = Path(__file__).resolve().parent


def fetch(url, data=None, headers=None):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                # Never expose response bodies or credential-bearing request objects.
                raise RuntimeError(f"Remote request failed: HTTP {exc.code}") from None
            time.sleep(2 ** attempt)
    raise RuntimeError("Remote request failed")


def load_catalogue(config):
    shop = os.environ.get("SHOPIFY_SHOP", "").removesuffix(".myshopify.com")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", shop):
        raise ValueError("Set SHOPIFY_SHOP to your myshopify.com subdomain")
    host = f"https://{shop}.myshopify.com"
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        client_id = os.environ.get("SHOPIFY_CLIENT_ID")
        secret = os.environ.get("SHOPIFY_CLIENT_SECRET")
        if not client_id or not secret:
            raise ValueError("Configure Shopify app credentials in environment variables")
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": client_id, "client_secret": secret}).encode()
        auth = json.loads(fetch(host + "/admin/oauth/access_token", body,
                                {"Content-Type": "application/x-www-form-urlencoded"}))
        token = auth["access_token"]
    query = (ROOT / "catalogue.graphql").read_text()
    endpoint = host + f"/admin/api/{config['api_version']}/graphql.json"
    products, after = [], None
    while True:
        for attempt in range(5):
            result = json.loads(fetch(endpoint,
                json.dumps({"query": query, "variables": {"after": after}}).encode(),
                {"Content-Type": "application/json", "X-Shopify-Access-Token": token}))
            errors = result.get("errors", [])
            if errors and all(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 ** attempt)
                continue
            if errors:
                raise RuntimeError("Shopify GraphQL failed; check app scopes and API version")
            break
        else:
            raise RuntimeError("Shopify remained throttled")
        page = result["data"]["products"]
        products.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return products
        after = page["pageInfo"]["endCursor"]
        if not after:
            raise ValueError("Missing Shopify pagination cursor")


def norm(value):
    return " ".join(value.casefold().split())


def product_number(gid):
    if not re.fullmatch(r"gid://shopify/Product/\d+", gid):
        raise ValueError("Unexpected Shopify product reference")
    return gid.rsplit("/", 1)[1]


def references(product):
    values = []
    for field in ("complementary", "related"):
        raw = product.get(field)
        if not raw:
            continue
        refs = json.loads(raw["value"])
        if not isinstance(refs, list):
            raise ValueError("Recommendation metafield must contain a JSON list")
        values.extend(product_number(ref) for ref in refs)
    return list(dict.fromkeys(values))


def parse_feed(data):
    items = []
    for node in ET.fromstring(data).findall("./channel/item"):
        def field(name):
            return node.findtext(f"{{{G}}}{name}", "").strip()
        gid = field("id")
        match = re.fullmatch(r"shopify_AU_(\d+)_(\d+)-organic", gid)
        if not match:
            raise ValueError("Unexpected feed ID format; update parser rather than guessing")
        items.append({"id": gid, "product": match[1], "variant": match[2],
                      "color": norm(field("color")), "size": norm(field("size")),
                      "group": field("item_group_id"), "type": norm(field("product_type")),
                      "availability": field("availability")})
    if not items or len({x["id"] for x in items}) != len(items):
        raise ValueError("Feed is empty or contains duplicate IDs")
    return items


def role(item):
    leaf = item["type"].split(">")[-1].strip()
    return {"bras": "bra", "briefs": "bottom", "thongs": "bottom",
            "g-strings": "bottom", "garter belts": "garter",
            "babydolls": "babydoll", "camisoles": "camisole",
            "shorts": "shorts"}.get(leaf)


def generate(items, products, config):
    catalogue = {product_number(p["id"]): p for p in products}
    # Require current Shopify active/Online Store catalogue AND presence in primary feed.
    eligible = [x for x in items if x["product"] in catalogue]
    by_product = collections.defaultdict(list)
    for item in eligible:
        if item["availability"] == "in_stock":
            by_product[item["product"]].append(item)
    approved = {norm(x) for x in config["approved_ranges"]}
    overrides = config.get("range_by_product_id", {})
    allowed_roles = {frozenset(x) for x in [("bra", "bottom"), ("bra", "garter"),
                      ("bottom", "garter"), ("babydoll", "bottom"), ("camisole", "shorts")]}
    extra = {frozenset(str(v) for v in pair) for pair in config.get("extra_set_pairs", [])}
    blocked = {tuple(str(v) for v in pair) for pair in config.get("blocked_pairs", [])}
    def range_name(item):
        title = catalogue[item["product"]]["title"]
        return norm(overrides.get(item["product"], title.split()[0] if title else ""))
    def bundle(item):
        return ("bundle" in {norm(t) for t in catalogue[item["product"]]["tags"]}
                or "sets" in item["type"])
    by_style, by_range = collections.defaultdict(list), collections.defaultdict(list)
    for pid, variants in by_product.items():
        for item in variants:
            if item["group"]:
                by_style[item["group"]].append(item)
            name = range_name(item)
            if name in approved and item["color"] and not bundle(item) and role(item):
                by_range[(name, item["color"])].append(item)
    output, audit = {}, []
    limit = config.get("max_relationships", 30)
    if not isinstance(limit, int) or not 1 <= limit <= 30:
        raise ValueError("max_relationships must be between 1 and 30")
    for source in eligible:
        candidates = collections.defaultdict(list)
        def add(kind, target):
            if target["product"] != source["product"] and (source["product"], target["product"]) not in blocked:
                candidates[(kind, target["product"])].append(target["id"])
        if not bundle(source) and role(source):
            for target in by_range.get((range_name(source), source["color"]), []):
                if frozenset((role(source), role(target))) in allowed_roles:
                    add("part_of_set", target)
            for pair in extra:
                if source["product"] in pair:
                    for pid in pair - {source["product"]}:
                        for target in by_product.get(pid, []):
                            if not bundle(target) and source["color"] == target["color"]:
                                add("part_of_set", target)
        for target in by_style.get(source["group"], []):
            if (source["color"] and target["color"] and source["color"] != target["color"]
                    and source["size"] and source["size"] == target["size"]
                    and source["type"] == target["type"]):
                add("substitute", target)
        if source["type"].startswith("lingerie >"):
            for pid in references(catalogue[source["product"]]):
                for target in by_product.get(pid, []):
                    if target["type"] == "lingerie accessories > stockings":
                        add("accessory", target)
        # Interleave target products so one many-size garment cannot consume the limit.
        pools = [(kind, pid, sorted(set(ids))) for (kind, pid), ids in sorted(candidates.items())]
        relations = []
        for index in range(max((len(ids) for _, _, ids in pools), default=0)):
            for kind, pid, ids in pools:
                if index < len(ids):
                    relations.append((kind, ids[index]))
        if len(relations) > limit:
            audit.append({"id": source["id"], "issue": "relationship_limit", "omitted": len(relations) - limit})
        output[source["id"]] = relations[:limit]
    return output, audit


def write_xml(output, path):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Cloud Blvd Google relationships"
    ET.SubElement(channel, "link").text = "https://cloudblvd.com.au"
    ET.SubElement(channel, "description").text = "Supplemental product relationships"
    for source_id, relations in sorted(output.items()):
        if not relations:
            continue
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, f"{{{G}}}id").text = source_id
        for kind, target_id in relations:
            entry = ET.SubElement(item, f"{{{G}}}related_product")
            for name, value in (("relationship_type", kind), ("identifier_type", "id"), ("identifier", target_id)):
                ET.SubElement(entry, f"{{{G}}}{name}").text = value
    ET.indent(rss)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    ET.ElementTree(rss).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--feed-file", type=Path, help="Use a downloaded primary feed instead of the URL")
    parser.add_argument("--catalogue-json", type=Path, help="Use a saved product-node array for offline testing")
    parser.add_argument("--output", type=Path, default=Path("output/relationships.xml"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    data = args.feed_file.read_bytes() if args.feed_file else fetch(config["feed_url"])
    items = parse_feed(data)
    products = json.loads(args.catalogue_json.read_text()) if args.catalogue_json else load_catalogue(config)
    relations, audit = generate(items, products, config)
    counts = collections.Counter(k for values in relations.values() for k, _ in values)
    if not counts:
        raise RuntimeError("No relationships generated; existing output was preserved")
    write_xml(relations, args.output)
    report = {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "primary_items": len(items), "catalogue_products": len(products),
              "items_with_relationships": sum(bool(v) for v in relations.values()),
              "relationships": dict(counts), "approved_ranges": config["approved_ranges"],
              "review": audit}
    args.output.with_suffix(".audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "review"}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Feed generation failed: {exc}", file=sys.stderr)
        sys.exit(1)
