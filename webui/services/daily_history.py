"""Date-bounded unadjusted public daily bars (independent of Sina's trailing window)."""
import datetime as dt
from urllib.parse import urlencode

from webui.services.http_client import request_json


def fetch_tencent(symbol, start, end):
    # Split long requests: the server caps response length even with explicit bounds.
    cursor, stop = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    records = []
    while cursor <= stop:
        last = min(stop, cursor + dt.timedelta(days=500))
        query = urlencode({'param': f'{symbol},day,{cursor},{last},600'})
        data = request_json('https://web.ifzq.gtimg.cn/appstock/app/kline/kline?'+query,
                            headers={'Referer': 'https://gu.qq.com/', 'User-Agent': 'Mozilla/5.0'},
                            timeout=8, retries=1, trust_env=False)
        if not isinstance(data, dict) or str(data.get('code')) != '0':
            raise ValueError('腾讯日线接口未返回有效响应')
        block = data['data'].get(symbol, {}) if isinstance(data.get('data'), dict) else {}
        for row in block.get('day') or []:
            if isinstance(row, list) and len(row) >= 6 and cursor.isoformat() <= row[0] <= last.isoformat():
                records.append(dict(zip(('date', 'open', 'close', 'high', 'low', 'volume'), row)))
        cursor = last + dt.timedelta(days=1)
    return records
