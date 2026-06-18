import httpx, json, time, sys
from shapely.geometry import shape, box, mapping
from shapely.ops import unary_union
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import contextily as cx

OUT="/home/jcernuda/clio-agent/.clio/artifacts/ndp-wildfire-explore"
FIRE="https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0"
SMOKE="https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/NDGD_SmokeForecast_v1/FeatureServer/0"
AIR="https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/Air%20Now%20Current%20Monitor%20Data%20Public/FeatureServer/0"

def q(url, params, tries=5):
    params={**params,"f":"geojson"}
    for i in range(tries):
        try:
            r=httpx.get(url+"/query", params=params, timeout=60)
            if r.status_code==200:
                return r.json()
        except Exception as e:
            err=e
        time.sleep(1.5*(i+1))
    print("QUERY FAIL", url, file=sys.stderr); return {"features":[]}

# 1. Largest active wildfire in western US with a real perimeter
west=("US-CA","US-OR","US-WA","US-NV","US-AZ","US-ID","US-MT","US-NM","US-CO","US-UT")
where="attr_IncidentTypeCategory='WF' AND poly_GISAcres>0 AND attr_POOState IN (%s)"%(",".join("'%s'"%s for s in west))
fj=q(FIRE,{"where":where,"outFields":"poly_IncidentName,poly_GISAcres,attr_PercentContained,attr_FireCause,attr_POOState,attr_POOCounty,attr_FireDiscoveryDateTime,attr_PrimaryFuelModel,attr_TotalIncidentPersonnel","outSR":4326,"orderByFields":"poly_GISAcres DESC","resultRecordCount":1})
feats=fj.get("features",[])
if not feats:
    print("No western WF; falling back to any WF"); 
    fj=q(FIRE,{"where":"attr_IncidentTypeCategory='WF' AND poly_GISAcres>0","outFields":"*","outSR":4326,"orderByFields":"poly_GISAcres DESC","resultRecordCount":1}); feats=fj["features"]
fire_feat=feats[0]; A=fire_feat["properties"]
fire_geom=shape(fire_feat["geometry"])
name=A.get("poly_IncidentName"); acres=A.get("poly_GISAcres")
print(f"FIRE: {name} | {acres:,.0f} acres | {A.get('attr_PercentContained')}% contained | {A.get('attr_POOCounty')} Co, {A.get('attr_POOState')} | cause={A.get('attr_FireCause')} | fuel={A.get('attr_PrimaryFuelModel')} | personnel={A.get('attr_TotalIncidentPersonnel')}")

# bbox around fire, padded ~1.2 deg to catch downwind smoke/air
minx,miny,maxx,maxy=fire_geom.bounds
pad=1.2
env=dict(xmin=minx-pad,ymin=miny-pad,xmax=maxx+pad,ymax=maxy+pad,spatialReference={"wkid":4326})
geomparams={"geometry":json.dumps(env),"geometryType":"esriGeometryEnvelope","inSR":4326,"spatialRel":"esriSpatialRelIntersects","outSR":4326}

# 2. smoke forecast polygons intersecting region
sj=q(SMOKE,{**geomparams,"where":"1=1","outFields":"*","resultRecordCount":2000})
smoke_feats=sj.get("features",[])
print(f"SMOKE polygons in region: {len(smoke_feats)}")
# detect a concentration/category field
smoke_cat=None
if smoke_feats:
    props=smoke_feats[0]["properties"]
    for k in props:
        if any(t in k.lower() for t in ("smoke","grid","dn","concent","categ","value")):
            smoke_cat=k; break
    print("  smoke fields sample:", list(props.keys())[:12], "| using:", smoke_cat)

# 3. air quality monitors in region
aj=q(AIR,{**geomparams,"where":"1=1","outFields":"*","resultRecordCount":3000})
air_feats=aj.get("features",[])
print(f"AIR monitors in region: {len(air_feats)}")
aqi_field=None
if air_feats:
    props=air_feats[0]["properties"]
    for k in props:
        if "aqi" in k.lower(): aqi_field=k; break
    print("  air fields sample:", list(props.keys())[:14], "| AQI:", aqi_field)

# ---- build GeoDataFrames (web mercator for basemap) ----
fire_gdf=gpd.GeoDataFrame(geometry=[fire_geom],crs=4326).to_crs(3857)
layers={}
if smoke_feats:
    sg=[shape(f["geometry"]) for f in smoke_feats if f.get("geometry")]
    svals=[f["properties"].get(smoke_cat) for f in smoke_feats if f.get("geometry")]
    smoke_gdf=gpd.GeoDataFrame({"v":svals},geometry=sg,crs=4326).to_crs(3857)
    layers["smoke"]=smoke_gdf
if air_feats:
    ag=[shape(f["geometry"]) for f in air_feats if f.get("geometry")]
    avals=[f["properties"].get(aqi_field) for f in air_feats if f.get("geometry")]
    air_gdf=gpd.GeoDataFrame({"aqi":avals},geometry=ag,crs=4326).to_crs(3857)
    layers["air"]=air_gdf

# ---- render ----
fig,ax=plt.subplots(figsize=(12,12))
if "smoke" in layers:
    sm=layers["smoke"]
    try:
        sm.plot(ax=ax,column="v",cmap="Greys",alpha=0.35,linewidth=0,legend=False)
    except Exception:
        sm.plot(ax=ax,color="grey",alpha=0.25,linewidth=0)
fire_gdf.plot(ax=ax,facecolor="red",edgecolor="darkred",alpha=0.55,linewidth=2,zorder=5)
if "air" in layers:
    ag=layers["air"].copy()
    def aqi_color(v):
        try: v=float(v)
        except: return "#999999"
        return ("#00e400" if v<=50 else "#ffff00" if v<=100 else "#ff7e00" if v<=150 else "#ff0000" if v<=200 else "#8f3f97" if v<=300 else "#7e0023")
    ag["c"]=ag["aqi"].map(aqi_color)
    ag.plot(ax=ax,color=ag["c"],edgecolor="black",markersize=55,zorder=6)
try:
    cx.add_basemap(ax,source=cx.providers.CartoDB.Positron,attribution_size=6)
except Exception as e:
    print("basemap failed:",e)
ax.set_axis_off()
ax.set_title(f"Active wildfire downwind-impact brief: {name}\n{acres:,.0f} acres, {A.get('attr_PercentContained')}% contained — {A.get('attr_POOCounty')} County, {A.get('attr_POOState')}\nLayers: fire perimeter (red) · NWS smoke forecast (grey) · AirNow AQI monitors (colored)",fontsize=12)
leg=[Patch(facecolor="red",alpha=0.55,label="Fire perimeter"),
     Patch(facecolor="grey",alpha=0.35,label="Smoke forecast"),
     Patch(facecolor="#00e400",label="AQI Good"),Patch(facecolor="#ff7e00",label="AQI Unhealthy(SG)"),
     Patch(facecolor="#ff0000",label="AQI Unhealthy"),Patch(facecolor="#8f3f97",label="AQI Very Unhealthy")]
ax.legend(handles=leg,loc="lower left",fontsize=8,framealpha=0.9)
png=f"{OUT}/wildfire_downwind_{str(name).replace(' ','_')}.png"
plt.savefig(png,dpi=130,bbox_inches="tight")
print("SAVED:",png)
