"""
GWorkshop local server — serves the app and proxies requests to 40k.app.
Extracts content from React Server Component payloads.
Run: python server.py
Then open: http://localhost:8080
"""

import http.server
import urllib.request
import urllib.error
import json
import os
import re
from urllib.parse import urlparse, parse_qs

PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_rsc_raw(html):
    """Extract and join RSC push payloads from Next.js HTML."""
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.DOTALL)
    if not pushes:
        return ''
    raw = ''.join(pushes)
    # Unescape JS string escapes, but keep \" as a special marker
    # The content uses \" for literal quotes (like inch marks: 9\")
    raw = raw.replace('\\n', '\n')
    raw = raw.replace('\\\\', '\\')
    # Replace \" with " but track that some are inside content (like 9\")
    # We do the unescape but use a regex that can handle embedded quotes
    raw = raw.replace('\\"', '"')
    return raw


def extract_army_rules(html):
    """Extract army rules page content."""
    raw = get_rsc_raw(html)
    if not raw:
        return json.dumps({"error": "No RSC content found"})

    result = {"army_rule": {"name": "", "description": ""}, "extra_rules": []}

    # Find the main army rule name and description
    # Pattern: section title followed by card content
    # Army rule name comes from children of section title pattern
    name_match = re.search(r'SectionTitle.*?children":"([^"]+)".*?Card_card', raw, re.DOTALL)

    # Better: look for the first large paragraph block
    paragraphs = re.findall(r'"p","p-\d+",\{"children":\[?"(.*?)"', raw)
    if not paragraphs:
        paragraphs = re.findall(r'"p","p-\d+",\{"children":"([^"]+)"', raw)

    # Get rule name from Abilities_ability__name or first meaningful section title
    ability_names = re.findall(r'Abilities_ability__name[^"]*","children":"([^"]+)"', raw)
    section_titles = re.findall(r'Table_header__title[^"]*","children":"([^"]+)"', raw)

    # For army rules, the first big text block is typically the army rule description
    # Find all p-tag content
    all_p = []
    for match in re.finditer(r'"p","p-\d+",\{"children":(.*?)\}\]', raw):
        content = match.group(1)
        # Extract text from children (could be string or array)
        if content.startswith('"'):
            # Simple string
            text = content.strip('"')
            all_p.append(text)
        elif content.startswith('['):
            # Array of mixed content
            pieces = re.findall(r'"([^"]{2,})"', content)
            text = ''.join(p for p in pieces if not p.startswith('$') and not p.startswith('strong') and not p.startswith('children') and 'className' not in p and not p.startswith('p-'))
            if text:
                all_p.append(text)

    # Also get list items
    list_items = re.findall(r'"li","li-\d+",\{"children":"([^"]+)"', raw)

    # Build the army rule
    if all_p:
        result["army_rule"]["description"] = ' '.join(all_p[:3])

    # Find dread ability names/descriptions (numbered items)
    dread_items = re.findall(r'(\d+\s*-\s*[^"]+)', raw)
    for item in dread_items:
        result["extra_rules"].append(item)

    # Get the name from section titles or ability names
    # For Chaos Knights, it's "Harbingers of Dread"
    for title in section_titles:
        if title not in ('Unit Abilities', 'Core Abilities', 'Faction Abilities', 'Keywords', 'Costs'):
            result["army_rule"]["name"] = title
            break

    if not result["army_rule"]["name"] and ability_names:
        result["army_rule"]["name"] = ability_names[0]

    # Fallback: search for known patterns
    if not result["army_rule"]["name"]:
        name_search = re.search(r'"children":"(Harbingers of Dread|Oath of Moment|For the Greater Good|Synapse|Code of Honour|Waaagh!|Shadow in the Warp|Contagion|Strands of Fate)', raw)
        if name_search:
            result["army_rule"]["name"] = name_search.group(1)

    return json.dumps(result)


def extract_detachment(html):
    """Extract detachment rule, stratagems, and enhancements from structured RSC data."""
    raw = get_rsc_raw(html)
    if not raw:
        return json.dumps({"error": "No RSC content found"})

    result = {
        "rule": {"name": "", "description": ""},
        "stratagems": [],
        "enhancements": {}
    }

    # Stratagems are embedded as JSON objects with "stratagem":{...} pattern
    # We need to find the correct detachmentId for the page we're on.
    # Strategy: find the detachment name from the page title (H1), then match
    # it to a detachment entry to get its stratagem detachmentId.

    # Find the correct detachmentId for this page's detachment.
    # The RSC data contains: "id":"<DET_ID>","name":"<DETACHMENT_NAME>"
    # We match the page title to find the right ID.
    page_title_match = re.search(r'"h1"[^}]*"children":"([^"]+)"', raw)
    page_det_name = page_title_match.group(1) if page_title_match else ''

    det_id = None
    # Look for: "id":"UUID","name":"<page_det_name>" pattern
    if page_det_name:
        id_pattern = re.search(
            r'"id":"([a-f0-9-]{20,})","name":"' + re.escape(page_det_name) + r'"',
            raw
        )
        if id_pattern:
            det_id = id_pattern.group(1)

    # Fallback: find from the page slug in structured data
    if not det_id:
        slug_match = re.search(r'"key":"([a-z-]+)","name":"[^"]+","details":\{"rules":\[\{"rule":\{"id":"([^"]+)"', raw)
        if slug_match:
            det_id = slug_match.group(2)

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

            # Only include stratagems from the detected detachment
            if det_id and det_id_field and det_id_field.group(1) != det_id:
                continue

            if name:
                desc_parts = []
                if target: desc_parts.append(f"TARGET: {target.group(1)}")
                if when: desc_parts.append(f"WHEN: {when.group(1)}")
                if effect: desc_parts.append(f"EFFECT: {effect.group(1)}")
                if restriction and restriction.group(1): desc_parts.append(f"RESTRICTION: {restriction.group(1)}")

                # Determine phase from whenRules
                phase = 'all'
                if when:
                    w = when.group(1).lower()
                    if 'command phase' in w: phase = 'command'
                    elif 'movement phase' in w: phase = 'movement'
                    elif 'shooting phase' in w: phase = 'shooting'
                    elif 'charge phase' in w: phase = 'charge'
                    elif 'fight phase' in w: phase = 'fight'
                    elif 'end of' in w: phase = 'end'

                result["stratagems"].append({
                    "name": name.group(1),
                    "cost": int(cost.group(1)) if cost else 1,
                    "phase": phase,
                    "description": ' '.join(desc_parts).replace('**', '')
                })
        except Exception:
            pass

    # Detachment rule: look for "detachmentRule" or ability with a description
    # The rule is typically in an "ability" block related to the detachment
    rule_match = re.search(r'"detachmentAbility":\{[^}]*"name":"([^"]+)"[^}]*"rules":"([^"]+)"', raw)
    if not rule_match:
        # Try alternative: look for the first ability-like entry with "rules" text
        # that's not an enhancement or stratagem
        rule_match = re.search(r'"ability":\{[^}]*"name":"([^"]+)"[^}]*"rules":"([^"]+)"', raw)

    if rule_match:
        result["rule"] = {
            "name": rule_match.group(1),
            "description": rule_match.group(2).replace('**', '').replace('\\n', ' ')[:500]
        }
    else:
        # Fallback: find the detachment rule from the Abilities_ability pattern
        ability_names = re.findall(r'Abilities_ability__name[^"]*","children":"([^"]+)"', raw)
        if ability_names:
            result["rule"]["name"] = ability_names[0]
            # Find description near it
            idx = raw.find(ability_names[0])
            if idx > 0:
                after = raw[idx:idx + 2000]
                p_match = re.search(r'"p","p-\d+",\{"children":"([^"]+)"', after)
                if p_match:
                    result["rule"]["description"] = p_match.group(1)

    # Enhancements: look for enhancement objects with "name" and "rules"
    # Filter to only enhancements for THIS detachment using det_id
    for match in re.finditer(r'"name":"([^"]{3,50})","rules":"(\*\*(?:CHAOS KNIGHTS|WAR DOG)\*\*[^"]{10,500})"', raw):
        name = match.group(1)
        rules = match.group(2).replace('**', '')
        # Check if this enhancement belongs to this detachment
        region_start = max(0, match.start() - 300)
        region = raw[region_start:match.end() + 300]
        if det_id and det_id in region:
            desc = re.sub(r'^(?:CHAOS KNIGHTS|WAR DOG) model only\.\s*', '', rules)
            result["enhancements"][name] = {"description": desc}

    # If no enhancements matched with detachment ID, try matching by rendered names
    if not result["enhancements"]:
        rendered_enh_names = re.findall(r'Enhancement_enhancement__name[^"]*","children":"([^"]+)"', raw)
        if rendered_enh_names:
            for ename in rendered_enh_names:
                # Find this enhancement's rules
                enh_match = re.search(r'"name":"' + re.escape(ename) + r'","rules":"([^"]+)"', raw)
                if enh_match:
                    rules = enh_match.group(1).replace('**', '')
                    desc = re.sub(r'^(?:CHAOS KNIGHTS|WAR DOG) model only\.\s*', '', rules)
                    result["enhancements"][ename] = {"description": desc}
        else:
            for match in re.finditer(r'"name":"([A-Z][A-Za-z\' ]+)","rules":"(\*\*CHAOS KNIGHTS\*\* model only\.[^"]{10,400})"', raw):
                name = match.group(1)
                rules = match.group(2).replace('**', '')
                desc = re.sub(r'^CHAOS KNIGHTS model only\.\s*', '', rules)
                if len(desc) > 10:
                    result["enhancements"][name] = {"description": desc}

    # For the rule, also try to find it from the structured data
    if not result["rule"]["name"]:
        # Look for the rule entry that matches our detachment ID
        # Pattern: "rule":{"id":"...","name":"Malefic Surge","detachmentId":"<det_id>",...}
        if det_id:
            rule_match = re.search(r'"rule":\{"id":"[^"]+","name":"([^"]+)","detachmentId":"' + re.escape(det_id) + r'"', raw)
            if rule_match:
                result["rule"]["name"] = rule_match.group(1)
        
        # Also try: "name":"RuleName" near detachmentId in first part of data
        if not result["rule"]["name"]:
            for match in re.finditer(r'"name":"([A-Z][A-Za-z ]+)".*?"rules":"([^"]{20,})"', raw[:15000]):
                name = match.group(1)
                if name not in result["enhancements"] and 'model only' not in match.group(2):
                    result["rule"] = {"name": name, "description": match.group(2).replace('**', '')[:500]}
                    break

    # If we have a rule name but no description, try to get it from the page's rendered content
    if result["rule"]["name"] and not result["rule"]["description"]:
        # Look for the description in a T-block (RSC text content block)
        # Pattern: T<number>,<text content>
        for match in re.finditer(r'\d+:T\d+,(.*?)(?=\n\d+:)', raw, re.DOTALL):
            text = match.group(1)
            if len(text) > 50 and result["rule"]["name"].lower().split()[0] in text.lower()[:50]:
                # Clean up: remove markdown bold, keep newlines for structure
                clean = text.replace('**', '').strip()
                result["rule"]["description"] = clean[:1500]
                break
        # Broader fallback: find the T-block that looks like a rule description
        if not result["rule"]["description"]:
            for match in re.finditer(r'\d+:T(\d+),(.*?)(?=\n\d+:)', raw, re.DOTALL):
                text = match.group(2)
                if len(text) > 100 and ('phase' in text.lower() or 'unit' in text.lower()):
                    clean = text.replace('**', '').strip()
                    result["rule"]["description"] = clean[:1500]
                    break

    return json.dumps(result)


def extract_unit(html):
    """Extract unit abilities from a datasheet page."""
    raw = get_rsc_raw(html)
    if not raw:
        return json.dumps({"error": "No RSC content found"})

    result = {
        "keywords": [],
        "abilities": [],
        "damaged": ""
    }

    # Extract keywords
    kw_section = re.search(r'Keywords.*?children":\[(.*?)\]', raw, re.DOTALL)
    # Better: find keyword tags
    keywords = re.findall(r'Keyword_keyword[^"]*","children":"([^"]+)"', raw)
    if not keywords:
        # Fallback: look for Keywords section title then grab nearby short ALL-CAPS strings
        kw_idx = raw.find('"Keywords"')
        if kw_idx < 0:
            kw_idx = raw.find('"children":"Keywords"')
        if kw_idx > 0:
            kw_region = raw[kw_idx:kw_idx + 2000]
            # Find all short strings that look like keywords (title case or caps, no spaces typically)
            potential = re.findall(r'"children":"([A-Z][A-Za-z ]+?)"', kw_region)
            keywords = [k for k in potential if k not in ('Keywords', 'Costs', 'Unit Composition', 'Core Abilities', 'Faction Abilities', 'Unit Abilities') and len(k) < 30]
    if keywords:
        result["keywords"] = keywords

    # Extract unit abilities
    # Each ability follows this pattern in the RSC stream:
    # Abilities_ability__name__XXX","children":"NAME"  (the ability name)
    # followed by a p-tag with either:
    #   "children":"simple text"  OR
    #   "children":["text",["$","strong",...,{"children":"KEYWORD"}],"more text"]
    
    ability_markers = list(re.finditer(r'Abilities_ability__name__[^"]*","children":"([^"]+)"', raw))

    for i, marker in enumerate(ability_markers):
        ab_name = marker.group(1)

        # Block: from after this marker to before the next ability marker
        block_start = marker.end()
        block_end = ability_markers[i + 1].start() - 50 if i + 1 < len(ability_markers) else block_start + 2000
        block = raw[block_start:block_end]

        # Find the p-tag children in this block
        desc = ''
        p_idx = block.find('"p","p-')
        if p_idx >= 0:
            # Find what follows "children": — either a string or an array
            children_idx = block.find('"children":', p_idx)
            if children_idx >= 0:
                after_children = block[children_idx + 11:].lstrip()
                if after_children.startswith('"'):
                    # Simple string: "children":"text here"
                    end_quote = after_children.find('"', 1)
                    if end_quote > 0:
                        desc = after_children[1:end_quote]
                elif after_children.startswith('['):
                    # Array: need to find matching ] accounting for nesting
                    depth = 0
                    arr_end = 0
                    for ci, ch in enumerate(after_children):
                        if ch == '[': depth += 1
                        elif ch == ']': 
                            depth -= 1
                            if depth == 0:
                                arr_end = ci
                                break
                    if arr_end > 0:
                        arr_content = after_children[1:arr_end]
                        # Now extract text: plain strings + strong children, in order
                        parts = []
                        # Match plain text strings (between array elements)
                        # These look like: ,"text here", or start of array "text here",
                        for piece in re.finditer(r'(?:^|,)\s*"((?:[^"\\]|\\.)*)"\s*(?:,|\]|$)', arr_content):
                            val = piece.group(1).replace('\\"', '"')
                            if val.startswith('$') or val in ('strong', 'children') or re.match(r'^strong-\d+$', val) or val.startswith('p-'):
                                continue
                            if len(val) > 0:
                                parts.append((piece.start(), val))
                        # Match strong-tag children
                        for strong in re.finditer(r'"strong","strong-\d+",\{"children":"([^"]+)"\}', arr_content):
                            parts.append((strong.start(), strong.group(1)))
                        parts.sort(key=lambda x: x[0])
                        desc = ''.join(p[1] for p in parts)

        # Replace escaped inch marks
        desc = desc.replace('\\"', '"').replace('\\', '')
        if desc:
            result["abilities"].append({"name": ab_name, "description": desc})

    # If no abilities found via class names, try fallback
    if not result["abilities"]:
        # Look for "Unit Abilities" section header then find named abilities after it
        ua_idx = raw.find('Unit Abilities')
        if ua_idx > 0:
            after = raw[ua_idx:ua_idx + 5000]
            # Find ability name patterns: short title-case strings followed by descriptions
            for match in re.finditer(r'"children":"([A-Z][A-Za-z \']+)".*?"children":"([^"]{20,})"', after):
                name = match.group(1)
                desc = match.group(2)
                if 'className' not in desc and not desc.startswith('$'):
                    result["abilities"].append({"name": name, "description": desc})

    # Get damaged profile
    dmg_match = re.search(r'wounds remaining.*?"children":\[?"(.*?)"', raw, re.DOTALL)
    if not dmg_match:
        dmg_match = re.search(r'"children":"(While this model has \d+-\d+ wounds remaining[^"]+)"', raw)
    if dmg_match:
        result["damaged"] = dmg_match.group(1)

    return json.dumps(result)


class GWorkshopHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ('/proxy', '/api/proxy'):
            self.handle_proxy(parsed)
            return

        super().do_GET()

    def handle_proxy(self, parsed):
        params = parse_qs(parsed.query)
        target_url = params.get('url', [None])[0]
        page_type = params.get('type', ['raw'])[0]  # 'rules', 'detachment', 'unit', or 'raw'

        if not target_url:
            self.send_error(400, 'Missing url parameter')
            return

        if not target_url.startswith('https://www.40k.app/'):
            self.send_error(403, 'Only 40k.app URLs allowed')
            return

        try:
            req = urllib.request.Request(target_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            resp = urllib.request.urlopen(req, timeout=15)
            html = resp.read().decode('utf-8', errors='replace')

            # Extract structured content based on page type
            if page_type == 'rules':
                content = extract_army_rules(html)
            elif page_type == 'detachment':
                content = extract_detachment(html)
            elif page_type == 'unit':
                content = extract_unit(html)
            else:
                content = get_rsc_raw(html)

            body = content.encode('utf-8')
            content_type = 'application/json' if page_type in ('rules', 'detachment', 'unit') else 'text/plain'

            self.send_response(200)
            self.send_header('Content-Type', f'{content_type}; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except urllib.error.HTTPError as e:
            error_body = json.dumps({"error": f"Upstream error: {e.code} {e.reason}"}).encode()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
        except Exception as e:
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, format, *args):
        msg = format % args
        if '/proxy' in msg:
            print(f"  [proxy] {msg}")
        elif not any(x in msg for x in ['.js', '.css', '.ico']):
            print(f"  {msg}")


if __name__ == '__main__':
    print(f"\n  GWorkshop server running at http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop\n")
    server = http.server.HTTPServer(('0.0.0.0', PORT), GWorkshopHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()
