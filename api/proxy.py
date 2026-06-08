"""
Vercel serverless function - proxies and parses 40k.app pages.
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
    # Unescape JS string escapes, but keep \" as a special marker
    # The content uses \" for literal quotes (like inch marks: 9\")
    raw = raw.replace('\\n', '\n')
    raw = raw.replace('\\\\', '\\')
    # Replace \" with " but track that some are inside content (like 9\")
    # We do the unescape but use a regex that can handle embedded quotes
    raw = raw.replace('\\"', '"')
    return raw


def extract_army_rules(html):
    """Extract army rules page content using structured RSC data."""
    raw = get_rsc_raw(html)
    if not raw:
        return json.dumps({"error": "No RSC content found"})

    result = {"army_rule": {"name": "", "description": ""}}

    # Strategy: find rules with "name" and "containers" containing "textContent" fields
    # These are the actual gameplay rules (vs list-building rules like "Daemonic Pact")
    # The main army rule typically has an armyRuleId and multiple text containers

    # Find all named rules that have containers with textContent
    rules = []
    for match in re.finditer(r'"name":"([^"]{3,60})","containers":\[(.*?)\]', raw, re.DOTALL):
        name = match.group(1)
        containers_block = match.group(2)

        # Extract textContent from containers
        texts = re.findall(r'"textContent":"((?:[^"\\]|\\.)*)"', containers_block)
        if texts:
            # Clean up text
            combined = '\n'.join(t.replace('\\n', '\n').replace('**', '') for t in texts)
            rules.append({"name": name, "description": combined})

    # Pick the best rule: prefer the one with armyRuleId (not list-building rules)
    # Skip rules that are clearly list-building (mentions "points" costs or "Select Army Faction")
    gameplay_rules = []
    for rule in rules:
        desc_lower = rule["description"].lower()
        if 'select army faction' in desc_lower and 'pts' in desc_lower:
            continue  # List-building rule, skip
        if 'can include' in desc_lower and 'even if they do not have the faction keyword' in desc_lower:
            continue  # Allied detachment rule, skip
        gameplay_rules.append(rule)

    if gameplay_rules:
        # Use the last gameplay rule (usually the actual army rule, after pact-type rules)
        best = gameplay_rules[-1] if len(gameplay_rules) > 1 else gameplay_rules[0]
        result["army_rule"] = best
    elif rules:
        # Fallback to any rule found
        result["army_rule"] = rules[-1]

    # If no structured rules found, try the old approach
    if not result["army_rule"]["name"]:
        # Look for section titles
        section_titles = re.findall(r'Table_header__title[^"]*","children":"([^"]+)"', raw)
        ability_names = re.findall(r'Abilities_ability__name[^"]*","children":"([^"]+)"', raw)
        for title in section_titles:
            if title not in ('Unit Abilities', 'Core Abilities', 'Faction Abilities', 'Keywords', 'Costs'):
                result["army_rule"]["name"] = title
                break
        if not result["army_rule"]["name"] and ability_names:
            result["army_rule"]["name"] = ability_names[0]

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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        target_url = params.get('url', [None])[0]
        page_type = params.get('type', ['raw'])[0]

        if not target_url:
            self._respond(400, {"error": "Missing url parameter"})
            return

        if not target_url.startswith('https://www.40k.app/'):
            self._respond(403, {"error": "Only 40k.app URLs allowed"})
            return

        try:
            req = urllib.request.Request(target_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            resp = urllib.request.urlopen(req, timeout=15)
            html = resp.read().decode('utf-8', errors='replace')

            if page_type == 'rules':
                content = extract_army_rules(html)
            elif page_type == 'detachment':
                content = extract_detachment(html)
            elif page_type == 'unit':
                content = extract_unit(html)
            else:
                content = get_rsc_raw(html)
                self._respond_text(200, content)
                return

            self._respond_json(200, content)

        except urllib.error.HTTPError as e:
            self._respond(e.code, {"error": f"Upstream: {e.code} {e.reason}"})
        except Exception as e:
            self._respond(502, {"error": str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, code, content):
        # content is already a JSON string from the extract functions
        body = content.encode() if isinstance(content, str) else json.dumps(content).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(body)

    def _respond_text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
