import json, math
d = json.load(open('out-transit.json', encoding='utf-8'))
R = 6371008.8
def hav(a, b):
    t = math.pi / 180
    h = math.sin(((b[1] - a[1]) * t) / 2) ** 2 + math.cos(a[1] * t) * math.cos(b[1] * t) * math.sin(((a[0] - b[0]) * t) / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(h)))

tot = 0; nospans = 0; atcap = 0; pts_at_cap = 0
lens = []
for c in d['cities'].values():
    for r in c['routes']:
        tot += 1
        npts = sum(len(s) for s in r['spans'])
        if not r['spans']:
            nospans += 1
        if len(r['spans']) >= 4:
            atcap += 1
        if any(len(s) >= 80 for s in r['spans']):
            pts_at_cap += 1
        lens.append(sum(hav(s[i], s[i+1]) for s in r['spans'] for i in range(len(s)-1)) / 1000)
lens.sort()
print(f"routes {tot}")
print(f"  with zero geometry      : {nospans} ({nospans/tot*100:.1f}%)")
print(f"  hitting the 4-span cap  : {atcap} ({atcap/tot*100:.1f}%)")
print(f"  hitting the 80-pt cap   : {pts_at_cap} ({pts_at_cap/tot*100:.1f}%)")
q = lambda p: lens[min(len(lens)-1, int(len(lens)*p))]
print(f"  length km  p50 {q(.5):.1f}  p90 {q(.9):.1f}  max {lens[-1]:.1f}")
longest = sorted(((sum(hav(s[i],s[i+1]) for s in r['spans'] for i in range(len(s)-1))/1000, slug, r['name'] or r['ref'], len(r['spans']))
                  for slug, c in d['cities'].items() for r in c['routes']), reverse=True)[:8]
print("\nlongest routes (4-span cap truncates the biggest ones):")
for km, slug, nm, sp in longest:
    print(f"  {km:7.1f}km  {slug:<22} {nm[:38]:40} spans={sp}")
