#!/usr/bin/env python3
"""
DISCO — Companies House Real Lead Discovery + Enrichment
Runs as a GitHub Action nightly.

Phase 1: Discover real UK companies by SIC code via CH Advanced Search
Phase 2: Per company — fetch full profile (accounts type, SIC, employee count) + officers
Phase 3: Estimate turnover from accounts type + employee count combined heuristic
Phase 4: Write leads.json + enrichment.json

CH_API_KEY must be set as a GitHub Secret.
"""

import os, json, time, sys
from datetime import datetime, timezone
import requests

CH_API_KEY = os.environ.get('CH_API_KEY', '')
if not CH_API_KEY:
    print('ERROR: CH_API_KEY not set'); sys.exit(1)

CH_BASE = 'https://api.company-information.service.gov.uk'
S = requests.Session()
S.auth = (CH_API_KEY, '')
S.headers.update({'Accept': 'application/json', 'User-Agent': 'DISCO-Enrichment/3.0'})
NOW = datetime.now(timezone.utc).isoformat()

# ── Niche definitions
NICHES = {
    'machinery': {
        'sic_codes': ['28110','28120','28130','28140','28150','28210','28220','28230','28240',
                      '28250','28290','28300','28410','28490','28910','28920','28930','28940',
                      '28950','28960','28990','46610','46620','46630','46640','46650','46660','46690'],
        'likely_currencies': ['EUR','USD','JPY'],
        'sector_desc': 'Machinery / Equipment',
        'fx_reason': 'Machinery and equipment sector — high likelihood of EU, US or Japanese sourcing',
    },
    'textiles': {
        'sic_codes': ['13100','13200','13300','13910','13920','13930','13940','13950','13960',
                      '13990','14110','14120','14130','14190','14200','14310','14390',
                      '46410','46420','46160'],
        'likely_currencies': ['INR','CNY','USD','EUR'],
        'sector_desc': 'Textiles / Apparel',
        'fx_reason': 'Textiles sector — Indian, Chinese and EU sourcing is standard',
    },
    'food': {
        'sic_codes': ['10110','10120','10130','10200','10310','10320','10391','10392','10411',
                      '10412','10420','10511','10512','10519','10520','10611','10612','10620',
                      '10710','10720','10730','10810','10820','10831','10832','10840','10850',
                      '10860','10890','10910','10920','46310','46320','46330','46340','46350',
                      '46360','46370','46380','46390','46210','46220','46230','46240'],
        'likely_currencies': ['EUR','USD','BRL','AUD'],
        'sector_desc': 'Food / Drink Importers',
        'fx_reason': 'Food and drink wholesale — EU, US and commodity market currency exposure',
    },
    'aerospace': {
        'sic_codes': ['30300','30400','33160','33170','46690','29100','29200'],
        'likely_currencies': ['USD','EUR','SEK'],
        'sector_desc': 'Aerospace / Defence',
        'fx_reason': 'Aerospace and defence — globally priced in USD, EU supply chain in EUR',
    },
    'construction': {
        'sic_codes': ['28920','41100','41201','41202','42110','42120','42130','42210','42220',
                      '42910','42990','43110','43120','43130','43210','43220','43290','43310',
                      '43320','43330','43341','43342','43390','43910','43991','43999',
                      '46610','46620','46630'],
        'likely_currencies': ['EUR','CNY','USD'],
        'sector_desc': 'Construction Equipment',
        'fx_reason': 'Construction equipment — EU and Chinese manufacturing sourcing',
    },
}

FINANCE_ROLES_RANKED = [
    ('chief financial officer', 10), ('group cfo', 10), ('cfo', 9),
    ('group finance director', 9), ('finance director', 8), ('financial director', 8),
    ('director of finance', 8), ('commercial finance director', 8),
    ('head of finance', 7), ('financial controller', 6), ('finance manager', 5),
]

def role_rank(title):
    t = (title or '').lower()
    for role, rank in FINANCE_ROLES_RANKED:
        if role in t:
            return rank
    return 0

def format_ch_name(raw):
    if not raw:
        return ''
    if ',' in raw:
        parts = raw.split(',', 1)
        return f'{parts[1].strip().title()} {parts[0].strip().title()}'
    return raw.strip().title()

# ── Global API budget guard
# CH free tier: 600 req/min. We make ~3 calls per company.
# 20 companies/niche × 5 niches = 100 companies → ~300 calls.
# Abort the run if we exceed this to avoid exhausting the CH quota.
_api_calls = 0
_API_BUDGET = 350

def ch_get(path, params=None, retries=2):
    global _api_calls
    _api_calls += 1
    if _api_calls > _API_BUDGET:
        print(f'  !! API budget exhausted ({_API_BUDGET} calls) — skipping remaining')
        return None

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

def search_by_sic(sic_code, size=5):
    data = ch_get('/advanced-search/companies', {
        'sic_codes': sic_code,
        'company_status': 'active',
        'company_type': 'ltd,plc',
        'size': size,
    })
    return (data or {}).get('items', [])

def get_company_profile(cn):
    return ch_get(f'/company/{cn}')

def get_officers(cn):
    data = ch_get(f'/company/{cn}/officers', {'items_per_page': 50})
    if not data:
        return []
    return [o for o in data.get('items', []) if not o.get('resigned_on')]

# ── Turnover bands — conservative lower-quartile estimates.
# Using lower quartile rather than arithmetic midpoint avoids inflating
# scores for companies at the bottom of each band.
# Wide bands (group/large) are especially dangerous with midpoints.
# Format: (lower_estimate_£m, upper_estimate_£m)
ACCT_BANDS = {
    'micro-entity':            (0.05, 0.15),   # statutory threshold £150k
    'dormant':                 (0,    0),
    'total exemption small':   (0.1,  0.6),    # threshold £632k net
    'total exemption full':    (0.3,  2.0),
    'small':                   (0.5,  4.0),    # threshold £10.2m turnover
    'unaudited abridged':      (1.0,  5.0),
    'audited abridged':        (2.0,  8.0),
    'full':                    (5.0,  25.0),   # broad — lower quartile estimate
    'medium':                  (12.0, 36.0),   # threshold £36m
    'group':                   (20.0, 60.0),   # conservative — many small groups
    'large':                   (36.0, 100.0),  # threshold £36m turnover or 250 emp
}
# Employee count heuristic — manufacturing/wholesale ~£150-220k revenue per head
def emp_to_turnover(emp):
    if not emp or emp == 0:
        return None
    return round(emp * 0.17, 1)  # conservative £170k per employee

def estimate_turnover(profile):
    """
    Conservative turnover estimate from accounts type + employee count.
    Returns (value_in_£m, source_label, ev_level)
    Uses lower quartile of band, not midpoint, to avoid score inflation.
    """
    if not profile:
        return 2, 'ESTIMATE_NO_PROFILE', 'INFERRED'

    accounts     = profile.get('accounts', {})
    last_accts   = accounts.get('last_accounts', {})
    acct_type    = (last_accts.get('type') or '').strip().lower()
    emp          = profile.get('number_of_employees') or 0
    emp_est      = emp_to_turnover(emp)
    band         = ACCT_BANDS.get(acct_type)

    if band:
        band_low, band_high = band
        if band_low == 0 and band_high == 0:
            return 0, 'DORMANT', 'INFERRED'

        # Lower-quartile point of the band
        lq = round(band_low + (band_high - band_low) * 0.25, 1)

        if emp_est:
            # Blend: 70% band lower-quartile, 30% employee estimate
            # Clamp to band bounds
            blended = round(lq * 0.7 + emp_est * 0.3, 1)
            blended = max(band_low, min(band_high, blended))
            label   = f'ESTIMATE_{acct_type.upper().replace(" ", "_")}+EMP'
            return max(0.5, blended), label, 'INFERRED'

        label = f'ESTIMATE_{acct_type.upper().replace(" ", "_")}'
        return max(0.5, lq), label, 'INFERRED'

    # No matching band — use employee count alone if available
    if emp_est:
        return emp_est, 'ESTIMATE_EMPLOYEES', 'INFERRED'

    # Default — unknown small company
    return 2, 'ESTIMATE_DEFAULT', 'INFERRED'

def find_best_finance_contact(officers):
    best, best_rank = None, 0
    for o in officers:
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

def make_fx_signals(niche_key, company, turnover, turnover_source):
    niche = NICHES[niche_key]
    cn = company.get('company_number', '')
    signals = [{
        'label': f'{niche["sector_desc"]} — international purchasing likely',
        'value': '/'.join(niche['likely_currencies'][:2]),
        'ev': 'STRONG_SIGNAL',
        'conf': 0.72,
        'reason': niche['fx_reason'],
        'sourceName': 'Companies House SIC classification',
        'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{cn}',
        'retrievedAt': NOW,
    }]
    # Add turnover signal if we have a decent estimate
    if turnover and turnover >= 2:
        signals.append({
            'label': f'Est. turnover: ~£{turnover}m',
            'value': f'~£{turnover}m',
            'ev': 'INFERRED',
            'conf': 0.55,
            'reason': f'Estimated from Companies House accounts filing type ({turnover_source})',
            'sourceName': 'Companies House accounts metadata',
            'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{cn}/filing-history',
            'retrievedAt': NOW,
        })
    return signals

def build_lead(company, profile, niche_key, officers):
    cn = company.get('company_number', '')
    name = (company.get('company_name') or company.get('title') or '').title()
    sic_codes = (profile or company).get('sic_codes', company.get('sic_codes', []))
    primary_sic = sic_codes[0] if sic_codes else ''

    address = (profile or company).get('registered_office_address', {})
    region = (address.get('locality') or address.get('region') or
              address.get('postal_code') or 'UK').title()

    contact = find_best_finance_contact(officers)
    niche = NICHES[niche_key]

    # Real employee count from profile
    emp = (profile or {}).get('number_of_employees') or 0

    # Turnover estimate
    turnover, turnover_source, turnover_ev = estimate_turnover(profile)

    officer_list = [{
        'name': format_ch_name(o.get('name', '')),
        'role': o.get('officer_role', ''),
        'occupation': o.get('occupation', ''),
        'appointed': o.get('appointed_on', ''),
    } for o in officers[:10]]

    return {
        'id': f'{niche_key[:1]}_ch_{cn}',
        'name': name,
        'companyNumber': cn,
        'sic': primary_sic,
        'sicDesc': '',
        'region': region,
        'niche': niche_key,
        'curr': niche['likely_currencies'][:2],
        'turnover': turnover,
        'turnoverSource': turnover_source,
        'growth': None,
        'emp': emp,
        'companyStatus': (profile or company).get('company_status', 'active'),
        'incorporatedOn': (profile or company).get('date_of_creation', ''),
        'fd': contact.get('fd') or None,
        'fdRole': contact.get('fdRole') or None,
        'appointed': contact.get('appointed') or None,
        'appointedDate': contact.get('appointedDate') or '',
        'contactSource': contact.get('contactSource') or None,
        'contactConfidence': contact.get('contactConfidence') or 0,
        'fdEvidenceLevel': contact.get('fdEvidenceLevel') or 'INFERRED',
        'officers': officer_list,
        'fxSignals': make_fx_signals(niche_key, company, turnover, turnover_source),
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

def discover_and_enrich():
    print(f'DISCO Real Lead Discovery v3 — {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    print(f'Pulling real active UK companies from Companies House by SIC code...\n')

    all_leads = {}
    stats = {'found': 0, 'enriched': 0, 'partial': 0, 'with_fd': 0,
             'turnover_varied': 0}

    for niche_key, niche in NICHES.items():
        print(f'\n── {niche["sector_desc"].upper()} ──')
        niche_leads = {}
        seen = set()
        target = 20
        sic_codes = niche['sic_codes']
        per_sic = max(3, (target + len(sic_codes[:6]) - 1) // len(sic_codes[:6]))

        for sic in sic_codes[:6]:
            if len(niche_leads) >= target:
                break
            companies = search_by_sic(sic, per_sic)
            time.sleep(0.4)

            for company in companies:
                cn = company.get('company_number', '')
                if not cn or cn in seen:
                    continue
                seen.add(cn)
                name = (company.get('company_name') or company.get('title') or '').title()

                # Full company profile — gives us accounts type + employee count
                profile = get_company_profile(cn)
                time.sleep(0.3)

                # Officers
                officers = get_officers(cn)
                time.sleep(0.3)

                lead = build_lead(company, profile, niche_key, officers)
                niche_leads[lead['id']] = lead
                stats['found'] += 1

                acct_type = ''
                if profile:
                    acct_type = (profile.get('accounts', {})
                                 .get('last_accounts', {})
                                 .get('type') or '').strip()

                t_str = f'£{lead["turnover"]}m'
                if lead["turnover"] != 5 or acct_type:
                    stats['turnover_varied'] += 1

                if lead['fd']:
                    print(f'  ✓ {name} ({cn}) — FD: {lead["fd"]} | {t_str} | {acct_type or "?"}')
                    stats['with_fd'] += 1
                    stats['enriched'] += 1
                else:
                    print(f'  · {name} ({cn}) — no FD | {t_str} | {acct_type or "?"}')
                    stats['partial'] += 1

                if len(niche_leads) >= target:
                    break

        all_leads.update(niche_leads)
        print(f'  → {len(niche_leads)} leads')

    # Write leads.json
    with open('leads.json', 'w') as f:
        json.dump({
            'generatedAt': NOW,
            'source': 'Companies House Advanced Search + Profile',
            'totalLeads': len(all_leads),
            'withFD': stats['with_fd'],
            'niches': list(NICHES.keys()),
            'leads': all_leads,
        }, f, indent=2)

    # Write enrichment.json — overlay used by frontend on load
    # IMPORTANT: must include turnover/emp so calcOpportunityScore is stable after refresh
    with open('enrichment.json', 'w') as f:
        json.dump({
            'generatedAt': NOW,
            'leadCount': len(all_leads),
            'enriched': stats['enriched'],
            'partial': stats['partial'],
            'failed': 0,
            'leads': {id: {
                'id':              l['id'],
                'companyNumber':   l['companyNumber'],
                'fd':              l['fd'],
                'fdRole':          l['fdRole'],
                'appointed':       l['appointed'],
                'contactSource':   l['contactSource'],
                'fdEvidenceLevel': l['fdEvidenceLevel'],
                'officers':        l['officers'],
                'fxSignals':       l['fxSignals'],
                'sources':         l['sources'],
                'enrichedAt':      l['enrichedAt'],
                'status':          l['enrichStatus'],
                # Include financials so opportunity score is stable after refresh
                'turnover':        l['turnover'],
                'turnoverSource':  l['turnoverSource'],
                'emp':             l['emp'],
            } for id, l in all_leads.items()},
        }, f, indent=2)

    print(f'\n{"="*50}')
    print(f'DONE: {stats["found"]} leads across {len(NICHES)} niches')
    print(f'  FD identified: {stats["with_fd"]}')
    print(f'  Turnover varied (non-default): {stats["turnover_varied"]}')
    print(f'  Written: leads.json + enrichment.json')

if __name__ == '__main__':
    discover_and_enrich()
