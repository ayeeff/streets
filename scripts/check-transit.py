import json, math, collections

d = json.load(open('out-transit.json', encoding='utf-8'))
R = 6371008.8
def hav(a, b):
    t = math.pi / 180
    h = math.sin(((b[1] - a[1]) * t) / 2) ** 2 + math.cos(a[1] * t) * math.cos(b[1] * t) * math.sin(((a[0] - b[0]) * t) / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(h)))

for slug, c in d['cities'].items():
    n = c['counts']
    print(f"\n{slug}  {c['name']}: routes={n['routes']} stops={n['stops']} modes={n['byMode']} operators={n['operators']} networks={n['networks']}")
    for r in c['routes'][:4]:
        pts = [p for s in r['spans'] for p in s]
        length = sum(hav(s[i], s[i + 1]) for s in r['spans'] for i in range(len(s) - 1)) / 1000
        print(f"   {r['mode']:6} {(r['name'] or r['ref'])[:34]:36} spans={len(r['spans'])} pts={len(pts):3} {length:7.1f}km stops={r['stopCount']:3} op={(r['operator'] or '-')[:22]}")
        if pts:
            print(f"          first={pts[0]} last={pts[-1]}")

allm = collections.Counter()
tot = 0
for c in d['cities'].values():
    allm.update(c['counts']['byMode'])
    tot += c['counts']['routes']
print(f"\ntotal routes {tot}  mode histogram {dict(allm)}")
print("cities with zero routes:", [k for k, v in d['cities'].items() if v['counts']['routes'] == 0])
