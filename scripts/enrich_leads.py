#!/usr/bin/env python3
"""
DISCO — Companies House Real Lead Discovery + Enrichment
Runs as a GitHub Action nightly.

Phase 1: Discover real UK companies by SIC code via CH Advanced Search
Phase 2: Enrich each company — officers, profile, FX signals
Phase 3: Write leads.json + enrichment.json for DISCO to load

CH_API_KEY must be set as a GitHub Secret.
"""

import os, json, time, re, sys
from datetime import datetime, timezone
import requests

CH_API_KEY = os.environ.get('CH_API_KEY', '')
if not CH_API_KEY:
    print('ERROR: CH_API_KEY not set'); sys.exit(1)

CH_BASE = 'https://api.company-information.service.gov.uk'
S = requests.Session()
S.auth = (CH_API_KEY, '')
S.headers.update({'Accept': 'application/json', 'User-Agent': 'DISCO-Enrichment/2.0'})

NOW = datetime.now(timezone.utc).isoformat()

# ── SIC code targeting by niche
# Each niche maps to CH SIC codes + expected currency exposure
NICHES = {
    'machinery': {
        'sic_codes': ['28110','28120','28130','28140','28150','28210','28220','28230','28240',
                      '28250','28290','28300','28410','28490','28910','28920','28930','28940',
                      '28950','28960','28990','46610','46620','46630','46640','46650','46660','46690'],
        'likely_currencies': ['EUR','USD','JPY'],
        'sector_desc': 'Machinery / Equipment',
        'fx_reason': 'Machinery and equipment sector — high likelihood of EU, US or Japanese sourcing'
    },
    'textiles': {
        'sic_codes': ['13100','13200','13300','13910','13920','13930','13940','13950','13960',
                      '13990','14110','14120','14130','14190','14200','14310','14390',
                      '46410','46420','46160'],
        'likely_currencies': ['INR','CNY','USD','EUR'],
        'sector_desc': 'Textiles / Apparel',
        'fx_reason': 'Textiles sector — Indian, Chinese and EU sourcing is standard'
    },
    'food': {
        'sic_codes': ['10110','10120','10130','10200','10310','10320','10391','10392','10411',
                      '10412','10420','10511','10512','10519','10520','10611','10612','10620',
                      '10710','10720','10730','10810','10820','10831','10832','10840','10850',
                      '10860','10890','10910','10920','46310','46320','46330','46340','46350',
                      '46360','46370','46380','46390','46210','46220','46230','46240'],
        'likely_currencies': ['EUR','USD','BRL','AUD'],
        'sector_desc': 'Food / Drink Importers',
        'fx_reason': 'Food and drink wholesale — EU, US and commodity market currency exposure'
    },
    'aerospace': {
        'sic_codes': ['30300','30400','33160','33170','46690','29100','29200'],
        'likely_currencies': ['USD','EUR','SEK'],
        'sector_desc': 'Aerospace / Defence',
        'fx_reason': 'Aerospace and defence — globally priced in USD, EU supply chain in EUR'
    },
    'construction': {
        'sic_codes': ['28920','41100','41201','41202','42110','42120','42130','42210','42220',
                      '42910','42990','43110','43120','43130','43210','43220','43290','43310',
                      '43320','43330','43341','43342','43390','43910','43991','43999',
                      '46610','46620','46630'],
        'likely_currencies': ['EUR','CNY','USD'],
        'sector_desc': 'Construction Equipment',
        'fx_reason': 'Construction equipment — EU and Chinese manufacturing sourcing'
    }
}

FINANCE_ROLES_RANKED = [
    ('Chief Financial Officer', 10), ('CFO', 10),
    ('Group Finance Director', 9), ('Group CFO', 9),
    ('Finance Director', 8), ('Financial Director', 8),
    ('Director of Finance', 8), ('Commercial Finance Director', 8),
    ('Head of Finance', 7),
    ('Financial Controller', 6),
    ('Finance Manager', 5),
]

def role_rank(title):
    t = (title or '').lower()
    for role, rank in FINANCE_ROLES_RANKED:
        if role.lower() in t:
            return rank
    return 0

def format_ch_name(raw):
    """CH returns 'SURNAME, Firstname' — reformat."""
    if not raw:
        return ''
    if ',' in raw:
        parts = raw.split(',', 1)
        surname = parts[0].strip().title()
        forename = parts[1].strip().title()
        return f'{forename} {surname}'
    return raw.strip().title()

def ch_get(path, params=None, retries=2):
    url = f'{CH_BASE}{path}'
    for attempt in range(retries + 1):
        try:
            r = S.get(url, params=params, timeout=12)
            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', 60))
                print(f'    Rate limited — waiting {wait}s')
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries:
                print(f'    CH error {path}: {e}')
                return None
            time.sleep(2)
    return None

def search_by_sic(sic_code, max_per_sic=8):
    """Search CH advanced search for active companies with a given SIC code."""
    params = {
        'sic_codes': sic_code,
        'company_status': 'active',
        'company_type': 'ltd,plc',
        'size': max_per_sic,
    }
    data = ch_get('/advanced-search/companies', params)
    if not data:
        return []
    return data.get('items', [])

def get_officers(company_number):
    data = ch_get(f'/company/{company_number}/officers', {'items_per_page': 50})
    if not data:
        return []
    return [o for o in data.get('items', []) if not o.get('resigned_on')]

def find_best_finance_contact(officers):
    best, best_rank = None, 0
    for o in officers:
        # CH uses officer_role (statutory) and occupation (job title) fields
        occupation = o.get('occupation', '')
        officer_role = o.get('officer_role', '')
        rk = role_rank(occupation) or role_rank(officer_role)
        if rk > best_rank:
            best_rank = rk
            best = o
    if best and best_rank >= 5:
        name = format_ch_name(best.get('name', ''))
        role = best.get('occupation') or best.get('officer_role', '')
        appointed = best.get('appointed_on', '')
        year = int(appointed[:4]) if appointed and len(appointed) >= 4 else None
        return {
            'fd': name,
            'fdRole': role.title() if role else 'Finance Director',
            'appointed': year,
            'appointedDate': appointed,
            'contactSource': 'CH_OFFICERS',
            'contactConfidence': 0.85,
            'fdEvidenceLevel': 'VERIFIED',
        }
    return {}

def get_filing_history(company_number):
    """Get most recent annual filing reference."""
    data = ch_get(f'/company/{company_number}/filing-history', {
        'items_per_page': 10,
        'category': 'accounts',
    })
    if not data:
        return None
    items = data.get('items', [])
    # Find most recent full/medium/small accounts
    preferred = ['AA', 'ACCOUNTS_WITH_ACCOUNTS_EXEMPTION_FILING', 'AA01']
    for item in items:
        if item.get('type') in preferred:
            return item
    return items[0] if items else None

def parse_turnover_from_accounts(company_number, filing):
    """
    Fetch the XBRL/iXBRL filing document and extract turnover.
    CH returns structured data via the document API for recent filings.
    Falls back to accounts category band if not parseable.
    """
    if not filing:
        return None, None

    # Try to get structured data from the accounts filing metadata
    # CH stores key financials in the filing description for some account types
    description = filing.get('description', '')
    desc_values = filing.get('description_values', {})

    # Some filings include made_up_date — useful for recency check
    made_up = filing.get('date', '') or desc_values.get('made_up_date', '')

    # Try the document API to get XBRL data
    links = filing.get('links', {})
    doc_url = links.get('document_metadata', '')
    if not doc_url:
        return None, made_up

    # Fetch document metadata
    doc_path = doc_url.replace('https://api.company-information.service.gov.uk', '')
    doc_data = ch_get(doc_path)
    if not doc_data:
        return None, made_up

    # Look for XBRL resource with financial data
    resources = doc_data.get('resources', {})
    for content_type, resource in resources.items():
        if 'xbrl' in content_type.lower() or 'xml' in content_type.lower():
            # Try to fetch the actual XBRL document
            xbrl_links = resource.get('links', {})
            xbrl_url = xbrl_links.get('self', '')
            if xbrl_url:
                try:
                    xbrl_path = xbrl_url.replace('https://api.company-information.service.gov.uk', '')
                    # Download the XBRL content
                    import re as _re
                    r = S.get(f'{CH_BASE}{xbrl_path}', timeout=10, stream=True)
                    if r.ok:
                        content = r.text[:50000]  # first 50k chars only
                        # Extract turnover from XBRL tags
                        patterns = [
                            r'<(?:uk-bus:|bus:|core:|xbrli:)?Turnover[^>]*>(\d+)</(?:uk-bus:|bus:|core:|xbrli:)?Turnover>',
                            r'<(?:uk-bus:|bus:|core:)?Revenue[^>]*>(\d+)</(?:uk-bus:|bus:|core:)?Revenue>',
                            r'TurnoverRevenue[^>]*>(\d+)<',
                            r'Turnover.*?>(\d{4,})<',
                        ]
                        for pat in patterns:
                            m = _re.search(pat, content, _re.IGNORECASE)
                            if m:
                                raw = int(m.group(1))
                                # Convert to £m — CH stores in full pounds
                                if raw > 1_000_000:
                                    return round(raw / 1_000_000, 1), made_up
                                elif raw > 1000:
                                    return round(raw / 1000, 1), made_up
                except Exception:
                    pass
    return None, made_up

def get_real_turnover(company_number, company):
    """
    Try to get actual turnover from filed accounts.
    Returns (turnover_in_millions, accounts_date, source_label)
    Falls back to accounts-category band estimate.
    """
    # First try from advanced search company data (accounts category)
    accounts = company.get('accounts', {})
    last_accounts = accounts.get('last_accounts', {})
    acct_type = (last_accounts.get('type') or '').lower()
    acct_date = last_accounts.get('made_up_to') or last_accounts.get('period_end_on') or ''

    # Band estimates by accounts type
    bands = {
        'micro-entity': 2,
        'dormant': 0,
        'small': 4,
        'total exemption small': 4,
        'total exemption full': 8,
        'group': 40,
        'full': 15,
        'medium': 20,
        'large': 75,
        'audited abridged': 10,
        'unaudited abridged': 5,
    }
    band_estimate = bands.get(acct_type, 5)

    # Try to get real figure from filing
    time.sleep(0.2)
    filing = get_filing_history(company_number)
    time.sleep(0.2)
    real_turnover, filing_date = parse_turnover_from_accounts(company_number, filing)

    if real_turnover and real_turnover > 0:
        date_label = filing_date or acct_date
        print(f'    Turnover: £{real_turnover}m (from accounts {date_label})')
        return real_turnover, date_label, 'CH_ACCOUNTS_VERIFIED'
    else:
        # Fall back to band estimate
        date_label = acct_date
        if band_estimate > 0:
            print(f'    Turnover: ~£{band_estimate}m (est. from {acct_type or "unknown"} accounts type)')
        return band_estimate, date_label, f'CH_ACCOUNTS_ESTIMATE_{acct_type.upper().replace(" ","_") or "UNKNOWN"}'

def make_fx_signals(niche_key, company):
    niche = NICHES[niche_key]
    sic_list = company.get('sic_codes', [])
    sic_str = ', '.join(sic_list)
    return [{
        'label': f'{niche["sector_desc"]} — international purchasing likely',
        'value': '/'.join(niche['likely_currencies'][:2]),
        'ev': 'STRONG_SIGNAL',
        'conf': 0.72,
        'reason': niche['fx_reason'],
        'sourceName': 'Companies House SIC classification',
        'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{company.get("company_number","")}',
        'retrievedAt': NOW,
    }]

def build_lead(company, niche_key, officers, turnover_data=None):
    cn = company.get('company_number', '')
    name = company.get('company_name', '') or company.get('title', '')
    sic_codes = company.get('sic_codes', [])
    primary_sic = sic_codes[0] if sic_codes else ''
    address = company.get('registered_office_address', {})
    region = address.get('locality') or address.get('region') or address.get('postal_code') or 'UK'

    # Finance contact
    contact = find_best_finance_contact(officers)
    niche = NICHES[niche_key]

    # Officer list for display
    officer_list = [{
        'name': format_ch_name(o.get('name', '')),
        'role': o.get('officer_role', ''),
        'occupation': o.get('occupation', ''),
        'appointed': o.get('appointed_on', ''),
    } for o in officers[:8]]

    # Use real turnover if provided, else fall back to band estimate
    if turnover_data:
        turnover, acct_date, turnover_source = turnover_data
    else:
        turnover, acct_date, turnover_source = get_real_turnover(cn, company)

    # Add turnover source signal if we got a real figure
    turnover_signal = []
    if turnover_source == 'CH_ACCOUNTS_VERIFIED':
        turnover_signal = [{
            'label': f'Turnover confirmed: £{turnover}m',
            'value': f'£{turnover}m',
            'ev': 'VERIFIED',
            'conf': 0.95,
            'reason': f'Extracted from filed accounts ({acct_date})',
            'sourceName': 'Companies House filed accounts',
            'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{cn}/filing-history',
            'retrievedAt': NOW,
        }]

    lead = {
        'id': f'{niche_key[:1]}_ch_{cn}',
        'name': name.title(),
        'companyNumber': cn,
        'sic': primary_sic,
        'sicDesc': '',
        'region': region.title(),
        'niche': niche_key,
        'curr': niche['likely_currencies'][:2],
        'turnover': turnover if turnover else 5,
        'turnoverSource': turnover_source,
        'accountsDate': acct_date,
        'growth': None,
        'emp': company.get('number_of_employees') or 0,
        'companyStatus': company.get('company_status', ''),
        'incorporatedOn': company.get('date_of_creation', ''),
        'fd': contact.get('fd') or None,
        'fdRole': contact.get('fdRole') or None,
        'appointed': contact.get('appointed') or None,
        'appointedDate': contact.get('appointedDate') or '',
        'contactSource': contact.get('contactSource') or None,
        'contactConfidence': contact.get('contactConfidence') or 0,
        'fdEvidenceLevel': contact.get('fdEvidenceLevel') or 'INFERRED',
        'officers': officer_list,
        'fxSignals': make_fx_signals(niche_key, company) + turnover_signal,
        'triggerEvents': [],
        'companySignals': [],
        'sources': [{
            'sourceName': 'Companies House',
            'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{cn}',
            'retrievedAt': NOW,
        }],
        'enrichedAt': NOW,
        'enrichStatus': 'ENRICHED' if contact.get('fd') else 'PARTIAL',
    }
    return lead

def discover_and_enrich():
    print(f'DISCO Real Lead Discovery — {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    print(f'Pulling real UK companies from Companies House...\n')

    all_leads = {}
    stats = {'found': 0, 'enriched': 0, 'partial': 0, 'with_fd': 0}

    for niche_key, niche in NICHES.items():
        print(f'\n── {niche["sector_desc"].upper()} ──')
        niche_leads = {}
        seen_companies = set()

        sic_codes = niche['sic_codes']
        # 20 per niche — 4 API calls per company (search, officers, filing, accounts)
        # keeps total under CH free tier rate limits
        target_per_niche = 20
        per_sic = max(3, target_per_niche // min(len(sic_codes), 6))

        for sic in sic_codes[:8]:  # top 8 SIC codes per niche
            if len(niche_leads) >= target_per_niche:
                break
            companies = search_by_sic(sic, per_sic)
            time.sleep(0.4)

            for company in companies:
                cn = company.get('company_number', '')
                if not cn or cn in seen_companies:
                    continue
                seen_companies.add(cn)

                name = (company.get('company_name') or company.get('title') or '').title()
                print(f'  {name} ({cn})')

                # Get officers
                officers = get_officers(cn)
                time.sleep(0.3)

                # Get real turnover from filed accounts
                turnover_data = get_real_turnover(cn, company)

                lead = build_lead(company, niche_key, officers, turnover_data)
                niche_leads[lead['id']] = lead
                stats['found'] += 1

                if lead['fd']:
                    print(f'    FD: {lead["fd"]} ({lead["fdRole"]})')
                    stats['with_fd'] += 1
                    stats['enriched'] += 1
                else:
                    stats['partial'] += 1

                if len(niche_leads) >= target_per_niche:
                    break

        all_leads.update(niche_leads)
        print(f'  → {len(niche_leads)} leads for {niche["sector_desc"]}')

    # Write leads.json — the full lead dataset for DISCO
    leads_output = {
        'generatedAt': NOW,
        'source': 'Companies House Advanced Search',
        'totalLeads': len(all_leads),
        'withFD': stats['with_fd'],
        'niches': list(NICHES.keys()),
        'leads': all_leads,
    }
    with open('leads.json', 'w') as f:
        json.dump(leads_output, f, indent=2)

    # Write enrichment.json — enrichment overlay (same data, different shape for compat)
    enrichment_output = {
        'generatedAt': NOW,
        'leadCount': len(all_leads),
        'enriched': stats['enriched'],
        'partial': stats['partial'],
        'failed': 0,
        'leads': {id: {
            'id': l['id'],
            'companyNumber': l['companyNumber'],
            'fd': l['fd'],
            'fdRole': l['fdRole'],
            'appointed': l['appointed'],
            'contactSource': l['contactSource'],
            'fdEvidenceLevel': l['fdEvidenceLevel'],
            'officers': l['officers'],
            'fxSignals': l['fxSignals'],
            'sources': l['sources'],
            'enrichedAt': l['enrichedAt'],
            'status': l['enrichStatus'],
        } for id, l in all_leads.items()},
    }
    with open('enrichment.json', 'w') as f:
        json.dump(enrichment_output, f, indent=2)

    print(f'\n{"="*50}')
    print(f'DONE: {stats["found"]} leads found across {len(NICHES)} niches')
    print(f'  FD identified: {stats["with_fd"]}')
    print(f'  Partial (company found, no FD title): {stats["partial"]}')
    print(f'  Written: leads.json + enrichment.json')

if __name__ == '__main__':
    discover_and_enrich()
