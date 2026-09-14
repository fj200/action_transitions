import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import os
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

save_lock = Lock()
OUT_PATH = 'analysis_2/ci_cd_actions_clean.csv'


def check_marketplace(action, session):
    owner, repo = action.split('/')[:2]
    url = f'https://github.com/{owner}/{repo}'
    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return {'action': action, 'is_marketplace': None,
                    'marketplace_url': None, 'status': f'http_{r.status_code}'}
        soup = BeautifulSoup(r.text, 'html.parser')
        market_link = soup.find('a', string=lambda s: s and 'view on marketplace' in s.lower())
        if market_link:
            href = market_link.get('href', '')
            marketplace_url = f'https://github.com{href}' if href.startswith('/') else href
            is_marketplace = '/marketplace/actions/' in marketplace_url
        else:
            marketplace_url = None
            is_marketplace = False
        return {'action': action, 'is_marketplace': is_marketplace,
                'marketplace_url': marketplace_url, 'status': 'ok'}
    except Exception as e:
        return {'action': action, 'is_marketplace': None,
                'marketplace_url': None, 'market_status': f'error: {e}'}


def build_marketplace_flags(df, n_threads=3):
    # All rows — no resume, redo everything in place
    actions = df['action'].tolist()

    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})

    results = {}
    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        futures = {executor.submit(check_marketplace, a, session): a for a in actions}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results[result['action']] = result
            print(f"  [{i}/{len(actions)}] {result['action']} → marketplace={result['is_marketplace']}")

            # Update in place and save after every result
            with save_lock:
                current = pd.read_csv(OUT_PATH)
                for action, r in results.items():
                    mask = current['action'] == action
                    current.loc[mask, 'is_marketplace']  = r['is_marketplace']
                    current.loc[mask, 'marketplace_url'] = r['marketplace_url']
                current.to_csv(OUT_PATH, index=False)

            time.sleep(0.5)

    final = pd.read_csv(OUT_PATH)
    print(f"\nupdated in place → {OUT_PATH}")
    print(f"  marketplace     : {final['is_marketplace'].sum()}")
    print(f"  not marketplace : {(final['is_marketplace']==False).sum()}")
    print(f"  unknown         : {final['is_marketplace'].isna().sum()}")
    return final


df = pd.read_csv(OUT_PATH)
marketplace_df = build_marketplace_flags(df, n_threads=5)