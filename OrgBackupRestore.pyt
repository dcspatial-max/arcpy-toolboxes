# -*- coding: utf-8 -*-
"""
Organization Backup & Restore (ArcGIS Pro Python Toolbox)

One backup + one restore for your ArcGIS Online / Enterprise content:
hosted feature services (saved as JSON definitions + data + attachments)
together with web maps, apps, dashboards, Survey123 forms, StoryMaps,
Experience Builder apps, Hub sites, notebooks and more.

  Tool 1 - Backup Organization
      <save location>/OrgBackup_YYYY-MM-DD/
          backup_log.txt                      item ID + name of everything saved
          manifest.json                       machine-readable list used by Restore
          Feature Service/<Title>_<id>/
              item.json, item_data.json, service.json, thumbnail
              layers/<id>.definition.json     fields, domains, symbology, relationships...
              layers/<id>.features.json       every feature/row (Esri JSON)
              attachments/<layer>/<objectid>/ photos, documents
          <Item Type>/<Title>_<id>/
              item.json, data.json (or the item's file), resources.zip,
              thumbnail, related.json

  Tool 2 - Restore from Backup
      Pick a backup folder, optionally pick individual items from a list, and
      choose a mode:
        * Restore in place   - same item IDs. Services: data is replaced.
                               Apps/maps: definition and resources are put back.
                               Items that no longer exist are re-created.
        * Restore as new copies - everything is re-created as new items.
      Services are restored first, then maps, then apps. Whenever an item has
      to be re-created, every later map/app that referenced it is re-pointed to
      the new item ID / service URL automatically.

ALWAYS test on non-production content first.
"""

import datetime
import json
import os
import re
import shutil
import tempfile
import traceback
import zipfile

import arcpy

try:
    from arcgis.gis import GIS
    from arcgis.features import FeatureLayerCollection
except ImportError:  # pragma: no cover
    GIS = None
    FeatureLayerCollection = None


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
SCOPE_MINE = "Only items I own"
SCOPE_ORG = "All items in the organization"

MODE_IN_PLACE = "Restore in place (same item IDs)"
MODE_NEW = "Restore as new copies"

HFS = "Hosted Feature Service"
SERVICE_FOLDER = "Feature Service"

APP_TYPES = [
    "Web Map",
    "Web Scene",
    "Web Mapping Application",   # Instant Apps, Web AppBuilder, classic Story Maps
    "Dashboard",
    "Form",                      # Survey123
    "StoryMap",
    "Web Experience",            # Experience Builder
    "Web Experience Template",
    "Hub Site Application",
    "Hub Page",
    "Site Application",
    "Site Page",
    "QuickCapture Project",
    "Workforce Project",
    "Notebook",
    "Insights Workbook",
    "Application",
    "Mobile Application",
]
ALL_TYPES = [HFS] + APP_TYPES

FILE_TYPES = {"Form", "Notebook", "Code Attachment", "Insights Workbook Package"}
RELATIONSHIPS = ["Survey2Service", "Survey2Data", "Map2Service", "WMA2Code"]
KNOWN_SNAPSHOT_FILES = {"item.json", "data.json", "data.txt", "data.bin",
                        "resources.zip", "related.json", "item_data.json", "service.json"}

# Restore order: services -> maps -> apps -> sites
RESTORE_ORDER = {"Feature Service": 0, "Web Map": 1, "Web Scene": 1,
                 "Hub Page": 3, "Site Page": 3, "Hub Site Application": 4, "Site Application": 4}

SERVICE_CREATE_KEYS = ["serviceDescription", "hasStaticData", "maxRecordCount", "supportedQueryFormats",
                       "description", "copyrightText", "spatialReference", "initialExtent", "fullExtent",
                       "allowGeometryUpdates", "units", "xssPreventionInfo"]
DROP_LAYER_KEYS = ["currentVersion", "serviceItemId", "editingInfo", "isDataVersioned",
                   "isDataArchived", "isDataBranchVersioned", "isDataReplicaTracked",
                   "isCoGoEnabled", "adminLayerInfo", "lastEditDate", "sourceSchemaChangesAllowed",
                   "isView", "isUpdatableView", "viewDefinitionQuery", "definitionQuery",
                   "sourceLayerId", "sourceServiceItemId"]
COPYABLE_PROPS = ["title", "type", "typeKeywords", "description", "snippet", "tags",
                  "extent", "spatialReference", "accessInformation", "licenseInfo",
                  "culture", "url", "properties", "categories"]
BATCH_SIZE = 500
ITEM_ID_RE = re.compile(r"\{([0-9a-f]{32})\}\s*$", re.I)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def safe_name(text, max_len=80):
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "untitled")).strip(" ._")
    return (text or "untitled")[:max_len]


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.datetime.now().strftime(fmt)


def first_line(ex):
    return str(ex).splitlines()[0] if str(ex) else ex.__class__.__name__


def norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def write_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, default=str, indent=indent)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def connect(url, username, password):
    if GIS is None:
        raise RuntimeError("The ArcGIS API for Python (arcgis) is not available.")
    if username and password:
        return GIS(url, username, password)
    arcpy.AddMessage("No username/password supplied - using ArcGIS Pro's active portal sign-in.")
    return GIS("pro")


def connection_params():
    p_url = arcpy.Parameter(displayName="Organization URL", name="org_url",
                            datatype="GPString", parameterType="Required", direction="Input")
    p_url.value = "https://www.arcgis.com"
    p_user = arcpy.Parameter(displayName="Username (leave blank to use ArcGIS Pro sign-in)",
                             name="username", datatype="GPString",
                             parameterType="Optional", direction="Input")
    p_pass = arcpy.Parameter(displayName="Password", name="password",
                             datatype="GPStringHidden", parameterType="Optional", direction="Input")
    return [p_url, p_user, p_pass]


def check_credentials(parameters):
    user, pwd = parameters[1], parameters[2]
    if user.valueAsText and not pwd.valueAsText:
        pwd.setErrorMessage("Enter a password, or clear the username to use your ArcGIS Pro sign-in.")


def search_all(gis, query):
    try:
        res = gis.content.advanced_search(query=query, max_items=-1)
        return list(res.get("results", []))
    except Exception:
        return list(gis.content.search(query=query, max_items=10000))


def add_item(gis, props, data=None):
    try:
        return gis.content.add(item_properties=props, data=data)
    except AttributeError:
        folder = gis.content.folders.get()
        job = folder.add(item_properties=props, file=data)
        return job.result() if hasattr(job, "result") else job


def all_layers(flc):
    return list(flc.layers or []) + list(flc.tables or [])


def multivalue_list(param):
    if not param.valueAsText:
        return []
    return [v.strip().strip("'\"") for v in param.valueAsText.split(";") if v.strip()]


# ==========================================================================
# Toolbox
# ==========================================================================
class Toolbox(object):
    def __init__(self):
        self.label = "Organization Backup & Restore"
        self.alias = "OrgBackup"
        self.tools = [BackupOrganization, RestoreFromBackup]


# ==========================================================================
# Tool 1 - Backup
# ==========================================================================
class BackupOrganization(object):
    def __init__(self):
        self.label = "Backup Organization"
        self.description = ("Backs up hosted feature services (as JSON) and web maps, apps, "
                            "dashboards, Survey123 forms, etc. to a dated folder with a log.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = connection_params()

        p_out = arcpy.Parameter(displayName="Backup Save Location", name="out_folder",
                                datatype="DEFolder", parameterType="Required", direction="Input")

        p_scope = arcpy.Parameter(displayName="Which Items", name="scope", datatype="GPString",
                                  parameterType="Required", direction="Input")
        p_scope.filter.type = "ValueList"
        p_scope.filter.list = [SCOPE_MINE, SCOPE_ORG]
        p_scope.value = SCOPE_ORG

        p_types = arcpy.Parameter(displayName="Item Types to Back Up", name="item_types",
                                  datatype="GPString", parameterType="Required", direction="Input",
                                  multiValue=True)
        p_types.filter.type = "ValueList"
        p_types.filter.list = ALL_TYPES
        p_types.values = ALL_TYPES

        p_attach = arcpy.Parameter(displayName="Include feature attachments (photos, documents)",
                                   name="include_attachments", datatype="GPBoolean",
                                   parameterType="Optional", direction="Input")
        p_attach.value = True

        p_views = arcpy.Parameter(displayName="Include hosted feature layer views",
                                  name="include_views", datatype="GPBoolean",
                                  parameterType="Optional", direction="Input")
        p_views.value = False

        return params + [p_out, p_scope, p_types, p_attach, p_views]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        check_credentials(parameters)

    def execute(self, parameters, messages):
        url = parameters[0].valueAsText.strip().rstrip("/")
        gis = connect(url, parameters[1].valueAsText, parameters[2].valueAsText)
        me = gis.users.me
        out_root = parameters[3].valueAsText
        scope = parameters[4].valueAsText
        types = multivalue_list(parameters[5])
        include_attach = bool(parameters[6].value)
        include_views = bool(parameters[7].value)
        arcpy.AddMessage("Connected to {} as {}".format(gis.properties.get("name", url), me.username))

        run_folder = os.path.join(out_root, "OrgBackup_{}".format(now_str("%Y-%m-%d")))
        if os.path.exists(run_folder):
            run_folder += "_" + now_str("%H%M%S")
        os.makedirs(run_folder)
        arcpy.AddMessage("Backup folder: {}".format(run_folder))

        base = 'owner:"{}"'.format(me.username) if scope == SCOPE_MINE else "orgid:{}".format(gis.properties.id)

        # ---- Find items
        items, seen = [], set()
        if HFS in types:
            arcpy.AddMessage("Searching for hosted feature services...")
            for i in search_all(gis, '{} AND type:"Feature Service"'.format(base)):
                kw = i.typeKeywords or []
                if i.type != "Feature Service" or "Hosted Service" not in kw:
                    continue
                if not include_views and "View Service" in kw:
                    continue
                if i.id not in seen:
                    seen.add(i.id)
                    items.append(i)
        for t in types:
            if t == HFS:
                continue
            arcpy.AddMessage("Searching for '{}' items...".format(t))
            for i in search_all(gis, '{} AND type:"{}"'.format(base, t)):
                if i.type == t and i.id not in seen:
                    seen.add(i.id)
                    items.append(i)
        items.sort(key=lambda i: (RESTORE_ORDER.get(i.type, 2), i.type, (i.title or "").lower()))
        arcpy.AddMessage("Found {} item(s) to back up.".format(len(items)))

        # ---- Back up
        manifest, results = [], []
        arcpy.SetProgressor("step", "Backing up...", 0, max(len(items), 1), 1)
        for n, item in enumerate(items, start=1):
            arcpy.SetProgressorLabel("[{}/{}] {}".format(n, len(items), item.title))
            arcpy.AddMessage("[{}/{}] {} - {} ({})".format(n, len(items), item.type, item.title, item.id))
            rel_folder = os.path.join(safe_name(item.type), "{}_{}".format(safe_name(item.title, 60), item.id))
            folder = os.path.join(run_folder, rel_folder)
            try:
                if item.type == "Feature Service":
                    detail, warnings = backup_service(item, folder, include_attach)
                else:
                    detail, warnings = backup_app_item(item, folder)
                for w in warnings:
                    arcpy.AddWarning("    " + w)
                status = "OK" if not warnings else "PARTIAL: " + "; ".join(warnings)
                manifest.append({"id": item.id, "title": item.title, "type": item.type,
                                 "owner": item.owner, "folder": rel_folder, "url": item.url})
            except Exception as ex:
                detail, status = "", "FAILED: " + first_line(ex)
                arcpy.AddWarning("    " + status)
                arcpy.AddMessage(traceback.format_exc())
            results.append((item.id, item.title, item.type, status, detail))
            arcpy.SetProgressorPosition()
        arcpy.ResetProgressor()

        write_json(os.path.join(run_folder, "manifest.json"),
                   {"created": now_str(), "organization": url, "user": me.username,
                    "scope": scope, "items": manifest}, indent=2)
        log = write_backup_log(run_folder, url, me.username, scope, include_attach, results)
        failed = sum(1 for r in results if r[3].startswith("FAILED"))
        arcpy.AddMessage("Done. {} of {} items backed up. Log: {}".format(len(results) - failed, len(results), log))
        if failed:
            arcpy.AddWarning("{} item(s) failed - see the log.".format(failed))


def write_backup_log(run_folder, url, username, scope, include_attach, results):
    log = os.path.join(run_folder, "backup_log.txt")
    ok = [r for r in results if not r[3].startswith("FAILED")]
    failed = [r for r in results if r[3].startswith("FAILED")]
    with open(log, "w", encoding="utf-8") as f:
        f.write("Organization Backup\n")
        f.write("Run date:     {}\n".format(now_str()))
        f.write("Organization: {}\n".format(url))
        f.write("Run by:       {}\n".format(username))
        f.write("Scope:        {}\n".format(scope))
        f.write("Attachments:  {}\n".format("included" if include_attach else "not included"))
        f.write("Backed up:    {} of {}\n".format(len(ok), len(results)))
        f.write("=" * 130 + "\n\n")
        f.write("BACKED UP\n")
        f.write("{:<34}{:<48}{:<26}{}\n".format("Item ID", "Item Name", "Type", "Details"))
        f.write("-" * 130 + "\n")
        for item_id, title, itype, status, detail in ok:
            f.write("{:<34}{:<48}{:<26}{}\n".format(item_id, (title or "")[:46], itype[:24], detail))
            if status != "OK":
                f.write("{:<34}  ! {}\n".format("", status))
        if failed:
            f.write("\nFAILED\n")
            f.write("{:<34}{:<48}{:<26}{}\n".format("Item ID", "Item Name", "Type", "Reason"))
            f.write("-" * 130 + "\n")
            for item_id, title, itype, status, detail in failed:
                f.write("{:<34}{:<48}{:<26}{}\n".format(item_id, (title or "")[:46], itype[:24], status))
    return log


def save_thumbnail(item, folder):
    try:
        if item.thumbnail:
            item.download_thumbnail(save_folder=folder)
    except Exception:
        pass


def backup_service(item, folder, include_attach):
    """Feature service -> JSON. Returns (detail text, warnings)."""
    warnings = []
    flc = FeatureLayerCollection.fromitem(item)
    write_json(os.path.join(folder, "item.json"), dict(item))
    try:
        data = item.get_data()
        if data:
            write_json(os.path.join(folder, "item_data.json"), data)
    except Exception as ex:
        warnings.append("item data: {}".format(first_line(ex)))
    write_json(os.path.join(folder, "service.json"), dict(flc.properties))
    save_thumbnail(item, folder)

    n_layers = n_feat = n_att = 0
    for lyr in all_layers(flc):
        props = dict(lyr.properties)
        lid = props["id"]
        write_json(os.path.join(folder, "layers", "{}.definition.json".format(lid)), props)
        kwargs = {"where": "1=1", "out_fields": "*", "return_geometry": props.get("type") != "Table"}
        if props.get("hasZ"):
            kwargs["return_z"] = True
        if props.get("hasM"):
            kwargs["return_m"] = True
        fs_dict = lyr.query(**kwargs).to_dict()
        write_json(os.path.join(folder, "layers", "{}.features.json".format(lid)), fs_dict)
        count = len(fs_dict.get("features", []))
        arcpy.AddMessage("    layer {} '{}': {} record(s)".format(lid, props.get("name"), count))
        n_layers += 1
        n_feat += count
        if include_attach and props.get("hasAttachments"):
            try:
                n_att += backup_attachments(lyr, props, fs_dict, os.path.join(folder, "attachments", str(lid)))
            except Exception as ex:
                warnings.append("attachments for layer {}: {}".format(lid, first_line(ex)))
    detail = "{} layer(s), {} feature(s), {} attachment(s)".format(n_layers, n_feat, n_att)
    return detail, warnings


def backup_attachments(lyr, props, fs_dict, att_root):
    oid_field = props.get("objectIdField") or "OBJECTID"
    try:
        infos = [{"oid": a.get("PARENTOBJECTID"), "id": a.get("ID"), "name": a.get("NAME")}
                 for a in lyr.attachments.search(where="1=1")]
    except Exception:
        infos = []
        for feat in fs_dict.get("features", []):
            oid = feat["attributes"].get(oid_field)
            for a in lyr.attachments.get_list(oid=oid) or []:
                infos.append({"oid": oid, "id": a.get("id"), "name": a.get("name")})
    for a in infos:
        dest = os.path.join(att_root, str(a["oid"]))
        os.makedirs(dest, exist_ok=True)
        lyr.attachments.download(oid=a["oid"], attachment_id=a["id"], save_path=dest)
    if infos:
        write_json(os.path.join(att_root, "index.json"), infos)
    return len(infos)


def backup_app_item(item, folder):
    """Map/app/dashboard/survey... -> JSON + files. Returns (detail text, warnings)."""
    os.makedirs(folder, exist_ok=True)
    saved, warnings = ["item.json"], []
    write_json(os.path.join(folder, "item.json"), dict(item))

    if item.type in FILE_TYPES:
        try:
            path = item.download(save_path=folder)
            if path:
                saved.append(os.path.basename(path))
        except Exception as ex:
            warnings.append("file download: {}".format(first_line(ex)))
    else:
        try:
            data = item.get_data(try_json=True)
            if isinstance(data, (dict, list)) and data:
                write_json(os.path.join(folder, "data.json"), data, indent=2)
                saved.append("data.json")
            elif isinstance(data, bytes) and data:
                with open(os.path.join(folder, "data.bin"), "wb") as f:
                    f.write(data)
                saved.append("data.bin")
            elif isinstance(data, str) and data.strip() and not os.path.isfile(data):
                with open(os.path.join(folder, "data.txt"), "w", encoding="utf-8") as f:
                    f.write(data)
                saved.append("data.txt")
        except Exception as ex:
            warnings.append("data: {}".format(first_line(ex)))

    try:
        if item.resources.list():
            item.resources.export(save_path=folder, file_name="resources.zip")
            saved.append("resources.zip")
    except Exception as ex:
        warnings.append("resources: {}".format(first_line(ex)))

    save_thumbnail(item, folder)

    related = {}
    for rel in RELATIONSHIPS:
        for direction in ("forward", "reverse"):
            try:
                rel_items = item.related_items(rel, direction)
            except Exception:
                continue
            if rel_items:
                related["{}|{}".format(rel, direction)] = [
                    {"id": r.id, "title": r.title, "type": r.type} for r in rel_items]
    if related:
        write_json(os.path.join(folder, "related.json"), related, indent=2)
        saved.append("related.json")
    return ", ".join(saved), warnings


# ==========================================================================
# Tool 2 - Restore
# ==========================================================================
class RestoreFromBackup(object):
    def __init__(self):
        self.label = "Restore from Backup"
        self.description = ("Restores hosted feature services, maps and apps from a folder made "
                            "by Backup Organization, in place or as new copies.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = connection_params()

        p_folder = arcpy.Parameter(displayName="Backup Folder", name="backup_folder",
                                   datatype="DEFolder", parameterType="Required", direction="Input")

        p_items = arcpy.Parameter(
            displayName="Items to Restore (leave all unchecked to restore everything)",
            name="items", datatype="GPString", parameterType="Optional", direction="Input",
            multiValue=True)
        p_items.filter.type = "ValueList"
        p_items.filter.list = ["(choose a backup folder)"]

        p_mode = arcpy.Parameter(displayName="Restore Mode", name="mode", datatype="GPString",
                                 parameterType="Required", direction="Input")
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = [MODE_IN_PLACE, MODE_NEW]
        p_mode.value = MODE_NEW

        p_attach = arcpy.Parameter(displayName="Restore feature attachments", name="restore_attachments",
                                   datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_attach.value = True

        p_style = arcpy.Parameter(
            displayName="Restore symbology & popups on existing services (in place)",
            name="restore_style", datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_style.value = True

        p_confirm = arcpy.Parameter(
            displayName="I understand 'in place' replaces current service data and app definitions",
            name="confirm", datatype="GPBoolean", parameterType="Optional", direction="Input")
        p_confirm.value = False

        return params + [p_folder, p_items, p_mode, p_attach, p_style, p_confirm]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p_folder, p_items, p_mode = parameters[3], parameters[4], parameters[5]
        if p_folder.altered and not p_folder.hasBeenValidated:
            folder = p_folder.valueAsText
            labels = []
            if folder and os.path.isdir(folder):
                labels = [item_label(e) for e in scan_backup(folder)]
            p_items.filter.list = labels or ["(no backed-up items found)"]
            p_items.value = None
        in_place = p_mode.valueAsText == MODE_IN_PLACE
        parameters[7].enabled = in_place
        parameters[8].enabled = in_place

    def updateMessages(self, parameters):
        check_credentials(parameters)
        folder = parameters[3].valueAsText
        if folder and os.path.isdir(folder) and not scan_backup(folder):
            parameters[3].setErrorMessage("No backed-up items found - pick a folder made by Backup Organization.")
        if parameters[5].valueAsText == MODE_IN_PLACE and not parameters[8].value:
            parameters[8].setErrorMessage("Tick this box to confirm an in-place restore.")

    def execute(self, parameters, messages):
        url = parameters[0].valueAsText.strip().rstrip("/")
        gis = connect(url, parameters[1].valueAsText, parameters[2].valueAsText)
        me = gis.users.me
        root = parameters[3].valueAsText
        chosen = {m.group(1).lower() for m in (ITEM_ID_RE.search(v) for v in multivalue_list(parameters[4])) if m}
        mode = parameters[5].valueAsText
        restore_attach = bool(parameters[6].value)
        restore_style = bool(parameters[7].value)

        entries = scan_backup(root)
        if chosen:
            entries = [e for e in entries if e["id"].lower() in chosen]
        arcpy.AddMessage("Restoring {} item(s) - mode: {}".format(len(entries), mode))

        ctx = RemapContext()
        results = []
        arcpy.SetProgressor("step", "Restoring...", 0, max(len(entries), 1), 1)
        for n, e in enumerate(entries, start=1):
            arcpy.SetProgressorLabel("[{}/{}] {}".format(n, len(entries), e["title"]))
            arcpy.AddMessage("[{}/{}] {} - {} ({})".format(n, len(entries), e["type"], e["title"], e["id"]))
            try:
                existing = gis.content.get(e["id"]) if mode == MODE_IN_PLACE else None
                if mode == MODE_IN_PLACE and existing is None:
                    arcpy.AddWarning("    Original item no longer exists - re-creating it as a new item.")

                if e["is_service"]:
                    if existing is not None:
                        status = replace_service_data(existing, e["path"], restore_attach, restore_style)
                        new_id = existing.id
                    else:
                        new_item, status = create_service_from_backup(gis, e["path"], restore_attach)
                        new_id = new_item.id
                        ctx.add(e["id"], new_id, e.get("url"), new_item.url)
                else:
                    snap = load_app_snapshot(e["path"])
                    if existing is not None:
                        status = overwrite_app_item(existing, snap, ctx)
                        new_id = existing.id
                    else:
                        new_item, status = create_app_copy(gis, snap, ctx)
                        new_id = new_item.id
                    for w in app_warnings(e["type"], existing is None):
                        arcpy.AddWarning("    " + w)
                        status += " | NOTE: " + w
                results.append((e["id"], new_id, e["title"], e["type"], status))
            except Exception as ex:
                results.append((e["id"], "", e["title"], e["type"], "FAILED: " + first_line(ex)))
                arcpy.AddWarning("    FAILED: {}".format(first_line(ex)))
                arcpy.AddMessage(traceback.format_exc())
            arcpy.SetProgressorPosition()
        arcpy.ResetProgressor()

        stamp = now_str("%Y-%m-%d_%H%M%S")
        log = os.path.join(root, "restore_log_{}.txt".format(stamp))
        with open(log, "w", encoding="utf-8") as f:
            f.write("Organization Restore\n")
            f.write("Run date:     {}\n".format(now_str()))
            f.write("Organization: {}\n".format(url))
            f.write("Run by:       {}\n".format(me.username))
            f.write("Mode:         {}\n".format(mode))
            f.write("=" * 140 + "\n")
            f.write("{:<34}{:<34}{:<40}{:<24}{}\n".format("Backup Item ID", "Restored To Item ID", "Item Name", "Type", "Status"))
            f.write("-" * 140 + "\n")
            for r in results:
                f.write("{:<34}{:<34}{:<40}{:<24}{}\n".format(r[0], r[1], (r[2] or "")[:38], r[3][:22], r[4]))
        if ctx.id_map:
            write_json(os.path.join(root, "restore_id_map_{}.json".format(stamp)),
                       {"item_ids": ctx.id_map, "service_urls": ctx.url_map}, indent=2)
        failed = sum(1 for r in results if r[4].startswith("FAILED"))
        arcpy.AddMessage("Done. {} restored, {} failed. Log: {}".format(len(results) - failed, failed, log))


def scan_backup(root):
    """Find every backed-up item under root, in restore order."""
    entries = []
    for r, _, files in os.walk(root):
        if "item.json" not in files:
            continue
        props = read_json(os.path.join(r, "item.json"), {})
        if not props.get("id"):
            continue
        entries.append({"id": props["id"], "title": props.get("title") or "", "type": props.get("type") or "",
                        "url": props.get("url"), "path": r, "is_service": "service.json" in files})
    entries.sort(key=lambda e: (RESTORE_ORDER.get(e["type"], 2), e["type"], e["title"].lower()))
    return entries


def item_label(e):
    return "{}  [{}]  {{{}}}".format(e["title"], e["type"], e["id"])


class RemapContext(object):
    """Tracks re-created items so later maps/apps point at the new ones."""

    def __init__(self):
        self.id_map = {}
        self.url_map = {}

    def add(self, old_id, new_id, old_url=None, new_url=None):
        if old_id and new_id and old_id != new_id:
            self.id_map[old_id] = new_id
        if old_url and new_url and old_url.rstrip("/") != new_url.rstrip("/"):
            self.url_map[old_url.rstrip("/")] = new_url.rstrip("/")

    def apply(self, text):
        if not text:
            return text
        for old in sorted(self.url_map, key=len, reverse=True):
            text = text.replace(old, self.url_map[old])
        for old, new in self.id_map.items():
            text = text.replace(old, new)
        return text


# --------------------------------------------------------------------------
# Service restore
# --------------------------------------------------------------------------
def load_layer_defs(folder, service):
    defs = []
    for key, is_table in (("layers", False), ("tables", True)):
        for entry in service.get(key) or []:
            d = read_json(os.path.join(folder, "layers", "{}.definition.json".format(entry["id"])))
            if d:
                defs.append((d, is_table))
    return defs


def clean_layer_def(d):
    d = json.loads(json.dumps(d))
    for k in DROP_LAYER_KEYS:
        d.pop(k, None)
    return d


def unique_service_name(gis, base):
    base = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_")[:80] or "restored"
    name, n = base, 1
    while True:
        try:
            if gis.content.is_service_name_available(name, "featureService"):
                return name
        except Exception:
            return name
        n += 1
        name = "{}_{}".format(base, n)


def find_thumbnail(folder, props):
    name = os.path.basename(props.get("thumbnail") or "")
    path = os.path.join(folder, name) if name else None
    return path if path and os.path.exists(path) else None


def create_service_from_backup(gis, folder, restore_attach):
    service = read_json(os.path.join(folder, "service.json"), {})
    props = read_json(os.path.join(folder, "item.json"), {})
    item_data = read_json(os.path.join(folder, "item_data.json"))
    today = now_str("%Y-%m-%d")
    orig_title = props.get("title") or "Restored service"
    title = "{} (restored {})".format(orig_title, today)

    name = unique_service_name(gis, "{}_restored_{}".format(orig_title, today.replace("-", "")))
    create_params = {k: service[k] for k in SERVICE_CREATE_KEYS if k in service}
    create_params["name"] = name
    create_params["capabilities"] = "Create,Delete,Query,Update,Editing"
    tags = props.get("tags") or ["restored backup"]
    new_item = gis.content.create_service(
        name=name, service_type="featureService", create_params=create_params,
        item_properties={"title": title, "tags": ",".join(tags) if isinstance(tags, list) else tags,
                         "snippet": props.get("snippet") or "",
                         "description": props.get("description") or ""})
    arcpy.AddMessage("    Created service {} ({})".format(name, new_item.id))

    defs = load_layer_defs(folder, service)
    flc = FeatureLayerCollection.fromitem(new_item)
    flc.manager.add_to_definition({"layers": [clean_layer_def(d) for d, t in defs if not t],
                                   "tables": [clean_layer_def(d) for d, t in defs if t]})
    new_item = gis.content.get(new_item.id)
    flc = FeatureLayerCollection.fromitem(new_item)
    by_id = {l.properties["id"]: l for l in all_layers(flc)}

    added = failed = 0
    for d, _ in defs:
        lyr = by_id.get(d["id"])
        if lyr is None:
            arcpy.AddWarning("    Layer {} '{}' was not created - skipped.".format(d["id"], d.get("name")))
            continue
        a, f, _ = load_features(lyr, folder, d, restore_attach)
        added += a
        failed += f

    update_kwargs = {}
    if item_data:
        update_kwargs["item_properties"] = {"text": json.dumps(item_data)}
    thumb = find_thumbnail(folder, props)
    if thumb:
        update_kwargs["thumbnail"] = thumb
    if update_kwargs:
        try:
            new_item.update(**update_kwargs)
        except Exception as ex:
            arcpy.AddWarning("    Could not restore popups/thumbnail: {}".format(first_line(ex)))

    final_def = {k: service[k] for k in ("capabilities", "editorTrackingInfo") if service.get(k)}
    if final_def:
        try:
            flc.manager.update_definition(final_def)
        except Exception as ex:
            arcpy.AddWarning("    Could not restore capabilities/editor tracking: {}".format(first_line(ex)))

    status = "OK - new service, {} feature(s) loaded".format(added)
    if failed:
        status += ", {} failed".format(failed)
    return new_item, status


def replace_service_data(target, folder, restore_attach, restore_style):
    if target.type != "Feature Service":
        raise RuntimeError("Existing item is a '{}', not a feature service.".format(target.type))
    service = read_json(os.path.join(folder, "service.json"), {})
    defs = load_layer_defs(folder, service)
    flc = FeatureLayerCollection.fromitem(target)
    by_id = {l.properties["id"]: l for l in all_layers(flc)}

    plan, skipped = [], []
    for d, _ in defs:
        lyr = by_id.get(d["id"])
        if lyr is not None and norm(lyr.properties.get("name")) == norm(d.get("name")):
            plan.append((lyr, d))
        else:
            skipped.append(d.get("name"))
    if not plan:
        raise RuntimeError("No layers in the backup match the existing service.")
    for s in skipped:
        arcpy.AddWarning("    Layer '{}' not found in the existing service - skipped.".format(s))

    added = failed = 0
    for lyr, d in plan:
        arcpy.AddMessage("    Clearing layer '{}'".format(d.get("name")))
        try:
            lyr.manager.truncate()
        except Exception:
            lyr.delete_features(where="1=1")
        a, f, _ = load_features(lyr, folder, d, restore_attach)
        added += a
        failed += f
        if restore_style and d.get("drawingInfo"):
            try:
                lyr.manager.update_definition({"drawingInfo": d["drawingInfo"]})
            except Exception as ex:
                arcpy.AddWarning("    Could not restore symbology: {}".format(first_line(ex)))
    if restore_style:
        item_data = read_json(os.path.join(folder, "item_data.json"))
        if item_data:
            target.update(item_properties={"text": json.dumps(item_data)})

    status = "OK - replaced data in {} layer(s), {} feature(s) loaded".format(len(plan), added)
    if failed:
        status += ", {} failed".format(failed)
    if skipped:
        status += "; skipped: {}".format(", ".join(skipped))
    return status


def load_features(lyr, folder, layer_def, restore_attach):
    """Returns (added, failed, attachments_added)."""
    lid = layer_def["id"]
    feats = (read_json(os.path.join(folder, "layers", "{}.features.json".format(lid)), {}) or {}).get("features") or []
    arcpy.AddMessage("    Loading {} record(s) into '{}'".format(len(feats), layer_def.get("name")))
    if not feats:
        return 0, 0, 0

    oid_field = layer_def.get("objectIdField") or next(
        (f["name"] for f in layer_def.get("fields", []) if f.get("type") == "esriFieldTypeOID"), "OBJECTID")
    gp = layer_def.get("geometryProperties") or {}
    drop = {x.lower() for x in (oid_field, gp.get("shapeAreaFieldName"), gp.get("shapeLengthFieldName")) if x}
    gid_field = layer_def.get("globalIdField")
    use_gids = bool(gid_field)

    old_oids, adds = [], []
    for f in feats:
        attrs = f.get("attributes") or {}
        old_oids.append(attrs.get(oid_field))
        new_f = {"attributes": {k: v for k, v in attrs.items() if k.lower() not in drop}}
        if f.get("geometry"):
            new_f["geometry"] = f["geometry"]
        adds.append(new_f)

    oid_map, added, failed = {}, 0, 0
    for start in range(0, len(adds), BATCH_SIZE):
        batch = adds[start:start + BATCH_SIZE]
        batch_oids = old_oids[start:start + BATCH_SIZE]
        try:
            res = lyr.edit_features(adds=batch, use_global_ids=use_gids, rollback_on_failure=False)
        except Exception as ex:
            if not use_gids:
                raise
            arcpy.AddWarning("    Service would not keep original GlobalIDs ({}); "
                             "loading with new GlobalIDs.".format(first_line(ex)))
            use_gids = False
            for a in adds:
                for k in list(a["attributes"]):
                    if k.lower() == gid_field.lower():
                        del a["attributes"][k]
            res = lyr.edit_features(adds=batch, rollback_on_failure=False)
        for old, r in zip(batch_oids, res.get("addResults", [])):
            if r.get("success"):
                oid_map[old] = r.get("objectId")
                added += 1
            else:
                failed += 1
    if failed:
        arcpy.AddWarning("    {} record(s) failed to load.".format(failed))

    att_added = 0
    att_root = os.path.join(folder, "attachments", str(lid))
    if restore_attach and os.path.isdir(att_root) and layer_def.get("hasAttachments"):
        for old_oid in os.listdir(att_root):
            src_dir = os.path.join(att_root, old_oid)
            if not os.path.isdir(src_dir) or not old_oid.isdigit():
                continue
            new_oid = oid_map.get(int(old_oid))
            if new_oid is None:
                continue
            for fn in os.listdir(src_dir):
                try:
                    lyr.attachments.add(oid=new_oid, file_path=os.path.join(src_dir, fn))
                    att_added += 1
                except Exception as ex:
                    arcpy.AddWarning("    Attachment {} on feature {} failed: {}".format(fn, old_oid, first_line(ex)))
        arcpy.AddMessage("    {} attachment(s) restored".format(att_added))
    return added, failed, att_added


# --------------------------------------------------------------------------
# App / map restore
# --------------------------------------------------------------------------
def load_app_snapshot(folder):
    props = read_json(os.path.join(folder, "item.json"), {})
    snap = {"folder": folder, "props": props, "data_text": None, "file": None,
            "resources": None, "thumbnail": find_thumbnail(folder, props),
            "related": read_json(os.path.join(folder, "related.json"), {})}
    if os.path.exists(os.path.join(folder, "data.json")):
        snap["data_text"] = json.dumps(read_json(os.path.join(folder, "data.json")))
    elif os.path.exists(os.path.join(folder, "data.txt")):
        with open(os.path.join(folder, "data.txt"), encoding="utf-8") as f:
            snap["data_text"] = f.read()
    if os.path.exists(os.path.join(folder, "resources.zip")):
        snap["resources"] = os.path.join(folder, "resources.zip")
    if props.get("type") in FILE_TYPES:
        thumb = os.path.basename(snap["thumbnail"] or "")
        others = [f for f in os.listdir(folder)
                  if f not in KNOWN_SNAPSHOT_FILES and f != thumb and os.path.isfile(os.path.join(folder, f))]
        if others:
            snap["file"] = os.path.join(folder, others[0])
    return snap


def restore_resources(item, zip_path, ctx):
    tmp = tempfile.mkdtemp(prefix="res_restore_")
    count = 0
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        for r, _, files in os.walk(tmp):
            rel = os.path.relpath(r, tmp)
            folder_name = None if rel in (".", "") else rel.replace("\\", "/")
            for fn in files:
                fp = os.path.join(r, fn)
                if fn.lower().endswith(".json") and (ctx.id_map or ctx.url_map):
                    with open(fp, encoding="utf-8", errors="ignore") as fh:
                        txt = fh.read()
                    new_txt = ctx.apply(txt)
                    if new_txt != txt:
                        with open(fp, "w", encoding="utf-8") as fh:
                            fh.write(new_txt)
                try:
                    item.resources.add(file=fp, folder_name=folder_name)
                except Exception:
                    item.resources.update(file=fp, folder_name=folder_name, file_name=fn)
                count += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return count


def overwrite_app_item(target, snap, ctx):
    props = snap["props"]
    if target.type != props.get("type"):
        raise RuntimeError("Type mismatch: backup is '{}', item is '{}'.".format(props.get("type"), target.type))
    update_props, parts = {}, []
    for k in ("title", "snippet", "description", "tags", "accessInformation", "licenseInfo"):
        if props.get(k) is not None:
            update_props[k] = ",".join(props[k]) if k == "tags" and isinstance(props[k], list) else props[k]
    if snap["data_text"] is not None:
        update_props["text"] = ctx.apply(snap["data_text"])
        parts.append("definition")
    kwargs = {"item_properties": update_props}
    if snap["file"]:
        kwargs["data"] = snap["file"]
        parts.append("file")
    if snap["thumbnail"]:
        kwargs["thumbnail"] = snap["thumbnail"]
    target.update(**kwargs)
    if snap["resources"]:
        parts.append("{} resource(s)".format(restore_resources(target, snap["resources"], ctx)))
    return "OK - restored in place ({})".format(", ".join(parts) or "properties")


def create_app_copy(gis, snap, ctx):
    props = snap["props"]
    old_id = props["id"]
    new_props = {k: props[k] for k in COPYABLE_PROPS if props.get(k) is not None}
    new_props["title"] = "{} (restored {})".format(props.get("title"), now_str("%Y-%m-%d"))
    for k in ("tags", "typeKeywords", "categories"):
        if isinstance(new_props.get(k), list):
            new_props[k] = ",".join(new_props[k])
    if isinstance(new_props.get("extent"), list):
        new_props["extent"] = ",".join(",".join(str(c) for c in pt) for pt in new_props["extent"])
    text = ctx.apply(snap["data_text"]) if snap["data_text"] is not None else None
    if text is not None:
        new_props["text"] = text
    if new_props.get("url"):
        new_props["url"] = ctx.apply(new_props["url"])

    new_item = add_item(gis, new_props, data=snap["file"])
    ctx.add(old_id, new_item.id)

    # Point the item's own ID references (app URL, config) at the new item
    fix = {}
    if text and old_id in text:
        fix["text"] = text.replace(old_id, new_item.id)
    if new_props.get("url") and old_id in new_props["url"]:
        fix["url"] = new_props["url"].replace(old_id, new_item.id)
    kwargs = {"item_properties": fix} if fix else {}
    if snap["thumbnail"]:
        kwargs["thumbnail"] = snap["thumbnail"]
    if kwargs:
        new_item.update(**kwargs)

    parts = [p for p in ("definition" if text else "", "file" if snap["file"] else "") if p]
    if snap["resources"]:
        parts.append("{} resource(s)".format(restore_resources(new_item, snap["resources"], ctx)))

    # Re-link relationships (e.g. Survey123 form -> its feature service) to restored items
    for key, rel_items in (snap["related"] or {}).items():
        rel_type, direction = key.split("|") if "|" in key else (key.split(" ")[0], "forward")
        if direction != "forward":
            continue
        for r in rel_items:
            new_rel_id = ctx.id_map.get(r["id"], r["id"])
            other = gis.content.get(new_rel_id)
            if other is not None:
                try:
                    new_item.add_relationship(other, rel_type)
                except Exception:
                    pass
    return new_item, "OK - new item ({})".format(", ".join(parts) or "properties")


def app_warnings(item_type, created_new):
    w = []
    if not created_new:
        return w
    if item_type == "Form":
        w.append("Open the new Survey123 form in Survey123 Connect or the web designer and "
                 "republish to confirm it is linked to the right feature service.")
    if item_type in ("Hub Site Application", "Site Application"):
        w.append("New Hub/site copy needs its site URL/domain set up in Hub.")
    if item_type in ("StoryMap", "Web Experience"):
        w.append("Open the new copy in its builder and publish once to refresh it.")
    return w
