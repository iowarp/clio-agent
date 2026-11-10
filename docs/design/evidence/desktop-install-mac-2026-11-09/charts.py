import sys, matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
out = sys.argv[1]
phases = ["resolve","download","verify","install (attach+copy)","first-run prepare"]
colors = ["#999999","#4e79a7","#e15759","#f28e2b","#59a14f"]
rows = {
 "main today":                       [0.5, 69.4, 16.0, 10.7, 58.9],
 "PR #1966":                         [0.5, 69.4,  3.8, 11.8, 58.9],
 "A: streamed .aar (lzma)":          [0.5, 51.8,  2.7,  0.0, 58.9],
 "A+B: + concurrent fetch,\nbundled pnpm": [0.5, 92.6, 2.7, 0.0, 12.0],
 "A+B+C: + Office on first use":     [0.5, 56.7,  2.7, 0.0, 12.0],
}
fig, ax = plt.subplots(figsize=(10,4.2)); y=list(range(len(rows)))[::-1]
for (name, vals), yy in zip(rows.items(), y):
    left=0
    for i,v in enumerate(vals):
        ax.barh(yy, v, left=left, color=colors[i], label=phases[i] if yy==y[0] else None); left+=v
    ax.text(left+2, yy, f"{left:.0f} s", va="center", fontsize=9)
ax.set_yticks(y); ax.set_yticklabels(rows.keys(), fontsize=9); ax.set_xlabel("seconds (this M4 Mac mini, GitHub at 8.3 MB/s)")
ax.set_title("macOS --desktop first install, by phase (measured; A+B and A+B+C projected from measured parts)", fontsize=10)
ax.legend(ncol=5, fontsize=8, loc="upper center", bbox_to_anchor=(0.5,-0.18)); ax.set_xlim(0,190); fig.tight_layout(); fig.savefig(f"{out}/phases.png", dpi=130)
# time vs link speed
import numpy as np
B = np.array([0.5,0.8,1,2,4,8.3,15,30])
main = 0.5+577/B+27.5+np.maximum(336/B+8,58.9)
pr   = 0.5+577/B+15.5+np.maximum(336/B+8,58.9)
a    = 0.5+np.maximum(428/B,0)+2.7+np.maximum(336/B+8,58.9)
ab   = 0.5+(428+298+38+5)/B+14.7
abc  = 0.5+(428+38+5)/B+14.7
fig, ax = plt.subplots(figsize=(8,4.2))
for v,l in [(main,"main today (943 MB)"),(pr,"PR #1966"),(a,"A: streamed .aar"),(ab,"A+B (769 MB)"),(abc,"A+B+C (471 MB)")]:
    ax.plot(B, v/60, marker="o", label=l)
ax.axhline(20, color="grey", ls=":"); ax.text(0.55, 20.4, "user's report: ~20 min", fontsize=8)
ax.axvline(0.8, color="grey", ls=":"); ax.set_xscale("log"); ax.set_xticks(B); ax.set_xticklabels([str(b) for b in B])
ax.set_xlabel("effective download rate, MB/s"); ax.set_ylabel("minutes, first install"); ax.set_title("Model: total first-install time vs link speed", fontsize=10)
ax.legend(fontsize=8); ax.grid(alpha=.3); fig.tight_layout(); fig.savefig(f"{out}/time-vs-link.png", dpi=130)
for name,v in [("main",main),("1966",pr),("A",a),("AB",ab),("ABC",abc)]: print(name, " ".join(f"{b}:{x:.0f}s" for b,x in zip(B,v)))
