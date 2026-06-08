"""
Vercel serverless function — proxies and parses 40k.app pages.
Endpoint: /api/proxy?url=...&type=rules|detachment|unit
"""

from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error
import json
import re
from urllib.parse import parse_qs, urlparse


def get_rsc_raw(html):
    """Extract and join RSC push payloads from Next.js HTML."""
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.DOTALL)
    if not pushes:
        return ''
    raw = ''.join(pushes)
    raw = raw.replace('\\n', '\n')
    raw = raw.replace('\\\\', '\\')
    raw = raw.replace('\\"', '"')
    return raw


def extract_army_rules(html):
    """Extract army rules page content."""
    raw = get_rsc_raw(html)
    if not raw:
        return {"error": "No RSC content found"}

    result = {"army_rule": {"name": "", "description": ""}}

    # Find rule name from section titles
    section_titles = re.findall(r'Table_header__title[^"]*","children":"([^"]+)"', raw)
    ability_names = re.findall(r'Abilities_ability__name__[^"]*","children":"([^"]+)"', raw)

    # Get all paragraph text
    all_p = []
    for match in re.finditer(r'"p","p-\d+",\{"children":"([^"]+)"\}', raw):
        all_p.append(match.group(1))

    if all_p:
        result["army_rule"]["description"] = ' '.join(all_p[:3])

    # Get name
    for title in section_titles:
        if title not in ('Unit Abilities', 'Core Abilities', 'Faction Abilities', 'Keywords', 'Costs'):
            result["army_rule"]["name"] = title
            break

    if not result["army_rule"]["name"] and ability_names:
        result["army_rule"]["name"] = ability_names[0]

    if not result["army_rule"]["name"]:
        name_search = re.search(r'"children":"(Harbingers of Dread|Oath of Moment|For the Greater Good|Synapse|Code of Honour|Waaagh!|Shadow in the Warp|Contagion|Strands of Fate)', raw)
        if name_search:
            result["army_rule"]["name"] = name_search.group(1)

    return result


def extract_detachment(html):
    """Extract detachment rule, stratagems, and enhancements."""
    raw = get_rsc_raw(html)
    if not raw:
        return {"error": "No RSC content found"}

    result = {
        "rule": {"name": "", "description": ""},
        "stratagems": [],
        "enhancements": {}
    }

    # Find the correct detachmentId for this page
    page_title_match = re.search(r'"h1"[^}]*"children":"([^"]+)"', raw)
    page_det_name = page_title_match.group(1) if page_title_match else ''

    det_id = None
    if page_det_name:
        id_pattern = re.search(
            r'"id":"([a-f0-9-]{20,})","name":"' + re.escape(page_det_name) + r'"',
            raw
        )
        if id_pattern:
            det_id = id_pattern.group(1)

    if not det_id:
        slug_match = re.search(r'"key":"([a-z-]+)","name":"[^"]+","details":\{"rules":\[\{"rule":\{"id":"([^"]+)"', raw)
        if slug_match:
            det_id = slug_match.group(2)

    # Extract stratagems
    for match in re.finditer(r'"stratagem":\{([^}]{50,2000})\}', raw):
        block = '{' + match.group(1) + '}'
        try:
            name = re.search(r'"name":"([^"]+)"', block)
            cost = re.search(r'"cpCost":"(\d+)"', block)
            when = re.search(r'"whenRules":"([^"]+)"', block)
            effect = re.search(r'"effectRules":"([^"]+)"', block)
            target = re.search(r'"targetRules":"([^"]+)"', block)
            restriction = re.search(r'"restrictionRules":"([^"]*)"', block)
            det_id_field = re.search(r'"detachmentId":"([^"]+)"', block)

            if det_id and det_id_field and det_id_field.group(1) != det_id:
                continue

            if name:
                desc_parts = []
                if target:
                    desc_parts.append(f"TARGET: {target.group(1)}")
                if when:
                    desc_parts.append(f"WHEN: {when.group(1)}")
                if effect:
                    desc_parts.append(f"EFFECT: {effect.group(1)}")
                if restriction and restriction.group(1):
                    desc_parts.append(f"RESTRICTION: {restriction.group(1)}")

                phase = 'all'
                if when:
                    w = when.group(1).lower()
                    if 'command phase' in w:
                        phase = 'command'
                    elif 'movement phase' in w:
                        phase = 'movement'
                    elif 'shooting phase' in w:
                        phase = 'shooting'
                    elif 'charge phase' in w:
                        phase = 'charge'
                    elif 'fight phase' in w:
                        phase = 'fight'
                    elif 'end of' in w:
                        phase = 'end'

                result["stratagems"].append({
                    "name": name.group(1),
                    "cost": int(cost.group(1)) if cost else 1,
                    "phase": phase,
                    "description": ' '.join(desc_parts).replace('**', '')
                })
        except Exception:
            pass

    # Extract enhancements
    for match in re.finditer(r'"name":"([^"]{3,50})","rules":"(\*\*(?:CHAOS KNIGHTS|WAR DOG|[A-Z ]+)\*\*[^"]{10,500})"', raw):
        name = match.group(1)
        rules = match.group(2).replace('**', '')
        region_start = max(0, match.start() - 300)
        region = raw[region_start:match.end() + 300]
        if det_id and det_id in region:
            desc = re.sub(r'^(?:CHAOS KNIGHTS|WAR DOG|[A-Z ]+) model only\.\s*', '', rules)
            result["enhancements"][name] = {"description": desc}

    if not result["enhancements"]:
        rendered_enh_names = re.findall(r'Enhancement_enhancement__name[^"]*","children":"([^"]+)"', raw)
        if rendered_enh_names:
            for ename in rendered_enh_names:
                enh_match = re.search(r'"name":"' + re.escape(ename) + r'","rules":"([^"]+)"', raw)
                if enh_match:
                    rules = enh_match.group(1).replace('**', '')
                    desc = re.sub(r'^(?:CHAOS KNIGHTS|WAR DOG|[A-Z ]+) model only\.\s*', '', rules)
                    result["enhancements"][ename] = {"description": desc}

    # Extract detachment rule
    if det_id:
        rule_match = re.search(r'"rule":\{"id":"[^"]+","name":"([^"]+)","detachmentId":"' + re.escape(det_id) + r'"', raw)
        if rule_match:
            result["rule"]["name"] = rule_match.group(1)

    if not result["rule"]["name"]:
        for match in re.finditer(r'"name":"([A-Z][A-Za-z ]+)".*?"rules":"([^"]{20,})"', raw[:15000]):
            name = match.group(1)
            if name not in result["enhancements"] and 'model only' not in match.group(2):
                result["rule"] = {"name": name, "description": match.group(2).replace('**', '')[:500]}
                break

    # Get rule description from T-block
    if result["rule"]["name"] and not result["rule"]["description"]:
        for match in re.finditer(r'\d+:T\d+,(.*?)(?=\n\d+:)', raw, re.DOTALL):
            text = match.group(1)
            if len(text) > 50 and result["rule"]["name"].lower().split()[0] in text.lower()[:50]:
                clean = text.replace('**', '').strip()
                result["rule"]["description"] = clean[:1500]
                break
        if not result["rule"]["description"]:
            for match in re.finditer(r'\d+:T(\d+),(.*?)(?=\n\d+:)', raw, re.DOTALL):
                text = match.group(2)
                if len(text) > 100 and ('phase' in text.lower() or 'unit' in text.lower()):
                    clean = text.replace('**', '').strip()
                    result["rule"]["description"] = clean[:1500]
                    break

    return result


def extract_unit(html):
    """Extract unit abilities from a datasheet page."""
    raw = get_rsc_raw(html)
    if not raw:
        return {"error": "No RSC content found"}

    result = {
        "keywords": [],
        "abilities": [],
        "damaged": ""
    }

    # Extract keywords
    keywords = re.findall(r'Keyword_keyword[^"]*","children":"([^"]+)"', raw)
    if not keywords:
        kw_idx = raw.find('"children":"Keywords"')
        if kw_idx < 0:
            kw_idx = raw.find('"Keywords"')
        if kw_idx > 0:
            kw_region = raw[kw_idx:kw_idx + 2000]
            potential = re.findall(r'"children":"([A-Z][A-Za-z ]+?)"', kw_region)
            keywords = [k for k in potential if k not in ('Keywords', 'Costs', 'Unit Composition', 'Core Abilities', 'Faction Abilities', 'Unit Abilities') and len(k) < 30]
    if keywords:
        result["keywords"] = keywords

    # Extract unit abilities
    ability_markers = list(re.finditer(r'Abilities_ability__name__[^"]*","children":"([^"]+)"', raw))

    for i, marker in enumerate(ability_markers):
        ab_name = marker.group(1)

        block_start = marker.end()
        block_end = ability_markers[i + 1].start() - 50 if i + 1 < len(ability_markers) else block_start + 2000
        block = raw[block_start:block_end]

        desc = ''
        p_idx = block.find('"p","p-')
        if p_idx >= 0:
            children_idx = block.find('"children":', p_idx)
            if children_idx >= 0:
                after_children = block[children_idx + 11:].lstrip()
                if after_children.startswith('"'):
                    end_quote = after_children.find('"', 1)
                    if end_quote > 0:
                        desc = after_children[1:end_quote]
                elif after_children.startswith('['):
                    depth = 0
                    arr_end = 0
                    for ci, ch in enumerate(after_children):
                        if ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                arr_end = ci
                                break
                    if arr_end > 0:
                        arr_content = after_children[1:arr_end]
                        parts = []
                        for piece in re.finditer(r'(?:^|,)\s*"((?:[^"\\]|\\.)*)"\s*(?:,|\]|$)', arr_content):
                            val = piece.group(1).replace('\\"', '"')
                            if val.startswith('$') or val in ('strong', 'children') or re.match(r'^strong-\d+$', val) or val.startswith('p-'):
                                continue
                            if len(val) > 0:
                                parts.append((piece.start(), val))
                        for strong in re.finditer(r'"strong","strong-\d+",\{"children":"([^"]+)"\}', arr_content):
                            parts.append((strong.start(), strong.group(1)))
                        parts.sort(key=lambda x: x[0])
                        desc = ''.join(p[1] for p in parts)

        desc = desc.replace('\\"', '"').replace('\\', '')
        if desc:
            result["abilities"].append({"name": ab_name, "description": desc})

    # Fallback
    if not result["abilities"]:
        ua_idx = raw.find('Unit Abilities')
        if ua_idx > 0:
            after = raw[ua_idx:ua_idx + 5000]
            for match in re.finditer(r'"children":"([A-Z][A-Za-z \']+)".*?"children":"([^"]{20,})"', after):
                name = match.group(1)
                desc = match.group(2)
                if 'className' not in desc and not desc.startswith('$'):
                    result["abilities"].append({"name": name, "description": desc})

    return result


def fetch_40k_app(url):
    """Fetch a page from 40k.app."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })
    resp = urllib.request.urlopen(req, timeout=15)
    return resp.read().decode('utf-8', errors='replace')


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        target_url = params.get('url', [None])[0]
        page_type = params.get('type', ['raw'])[0]

        if not target_url:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Missing url parameter"}).encode())
            return

        if not target_url.startswith('https://www.40k.app/'):
            self.send_response(403)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Only 40k.app URLs allowed"}).encode())
            return

        try:
            html = fetch_40k_app(target_url)

            if page_type == 'rules':
                content = json.dumps(extract_army_rules(html))
            elif page_type == 'detachment':
                content = json.dumps(extract_detachment(html))
            elif page_type == 'unit':
                content = json.dumps(extract_unit(html))
            else:
                content = get_rsc_raw(html)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if page_type in ('rules', 'detachment', 'unit') else 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(content.encode())

        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Upstream: {e.code} {e.reason}"}).encode())
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
