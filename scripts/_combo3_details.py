import json
p = r'C:\Users\user\Desktop\HL\hyperliquid_bot\data\honest_replay\sweep_ib\003_atr1.1_rr2.2_sl0.005_vix-off_mm0.50.json'
d = json.load(open(p))
print('WINDOW:', d['history_window'])
print('COINS_ATTEMPTED:', d['coins_attempted'])
print('COINS_WITH_DATA:', d['coins_with_data'])
print('SKIPPED:', d['skipped_total'])
print()
print('CAPS_APPLIED:', d['caps_applied'])
print()
print('PER_SEC_TYPE:')
for s in d['per_sec_type']:
    print(' ', s)
print()
print('PER_PATTERN:')
for p in d['per_pattern']:
    print(' ', p)
print()
print('TOP_COIN_BY_R:')
for c in d['per_coin_top20'][:10]:
    print(' ', c)
