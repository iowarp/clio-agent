# The FV scenarios' model (ledger FV): a two-part duct whistle with 1.6 mm walls.
# uv run --no-project --python 3.12 --with cadquery python fileview_whistle.py <folder>
# (put the CadQuery environment in its own UV_CACHE_DIR and remove it afterwards: ~1.7 GB)
"""A two-part duct whistle: a hollow body with a windway and labium window, and a plug."""
import sys
import cadquery as cq

out = sys.argv[1]
L, W, H, WALL = 60.0, 20.0, 16.0, 1.6
# Body: a box with a resonator chamber and a windway along X.
body = cq.Workplane("XY").box(L, W, H, centered=(False, True, False))
chamber = (
    cq.Workplane("XY")
    .box(L - 20 - WALL, W - 2 * WALL, H - 2 * WALL, centered=(False, True, False))
    .translate((20, 0, WALL))
)
# The mouthpiece bore holds the plug; the gap left above it is the windway.
bore = (
    cq.Workplane("XY")
    .box(20.5, W - 2 * WALL, H - 2 * WALL, centered=(False, True, False))
    .translate((-0.5, 0, WALL))
)
window = cq.Workplane("XY").box(5, 10, WALL + 0.2, centered=(False, True, False)).translate((20, 0, H - WALL))
body = body.cut(chamber).cut(bore).cut(window)
# A lanyard lug with a 4 mm hole at the closed end.
lug = cq.Workplane("XZ").center(L + 5, H / 2).circle(6).circle(2).extrude(3, both=True)
body = body.union(lug)
# Plug: fills the bore below a 1.2 mm windway, with a grip outside the mouth.
plug = (
    cq.Workplane("XY")
    .box(20, W - 2 * WALL - 0.2, H - 2 * WALL - 1.2, centered=(False, True, False))
    .translate((0, 0, WALL))
)
plug = plug.union(cq.Workplane("XY").box(3, W, H, centered=(False, True, False)).translate((-3, 0, 0)))
assy = cq.Assembly(name="whistle")
assy.add(body, name="body", color=cq.Color(0.95, 0.55, 0.15))
assy.add(plug, name="plug", color=cq.Color(0.2, 0.45, 0.85))
assy.save(out + "/whistle_assembly.step")
print("ok")
