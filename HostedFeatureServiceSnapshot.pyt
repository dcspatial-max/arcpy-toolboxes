# -*- coding: utf-8 -*-
"""
Hosted Feature Service Snapshot (ArcGIS Pro Python Toolbox)

Walks every hosted feature service in your ArcGIS Online / Enterprise
organization (or only the ones you own), exports each one, downloads the
export to a dated snapshot folder, and writes a text log of what was
downloaded (item ID + name).

How the export works:
  ArcGIS can only download a hosted feature service by first exporting it to
  a temporary item (File Geodatabase, Shapefile, etc.) in your content. This
  tool creates that temporary export, downloads it, and then deletes it.

Permissions:
  - You can always export services you own.
  - To export services owned by others you must be an org Administrator, or
    the owner must have enabled "Allow others to export to different formats".
"""

import datetime
import json
import os
import re
import traceback

import arcpy

try:
    from arcgis.gis import GIS
except ImportError:  # pragma: no cover - only hit outside ArcGIS Pro
    GIS = None


EXPORT_FORMATS = ["File Geodatabase", "Shapefile", "GeoJson", "CSV", "Excel"]
SCOPE_MINE = "Only items I own"
SCOPE_ORG = "All items in the organization"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def safe_name(text, max_len=80):
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "untitled")).strip(" ._")
    return (text or "untitled")[:max_len]


def make_run_folder(root, prefix):
    """Create <root>/<prefix>_YYYY-MM-DD (adds _HHMMSS if already run today)."""
    now = datetime.datetime.now()
    folder = os.path.join(root, "{}_{}".format(prefix, now.strftime("%Y-%m-%d")))
    if os.path.exists(folder):
        folder = "{}_{}".format(folder, now.strftime("%H%M%S"))
    os.makedirs(folder)
    return folder


def connect(url, username, password):
    if GIS is None:
        raise RuntimeError("The ArcGIS API for Python (arcgis) is not available.")
    if username and password:
        return GIS(url, username, password)
    # Fall back to the portal you are signed in to in ArcGIS Pro (SAML/MFA users)
    arcpy.AddMessage("No username/password supplied - using ArcGIS Pro's active portal sign-in.")
    return GIS("pro")


def search_all(gis, query):
    """Return every item matching the query (no 10,000 cap where supported)."""
    try:
        res = gis.content.advanced_search(query=query, max_items=-1)
        return list(res.get("results", []))
    except Exception:
        return list(gis.content.search(query=query, max_items=10000))


def delete_item(item):
    try:
        item.delete(permanent=True)  # skip the recycle bin (newer API versions)
    except TypeError:
        item.delete()


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------
class Toolbox(object):
    def __init__(self):
        self.label = "Hosted Feature Service Snapshot"
        self.alias = "HFSSnapshot"
        self.tools = [SnapshotHostedFeatureServices]


class SnapshotHostedFeatureServices(object):
    def __init__(self):
        self.label = "Snapshot Hosted Feature Services"
        self.description = (
            "Exports and downloads every hosted feature service in the "
            "organization to a dated snapshot folder, with a text log."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_url = arcpy.Parameter(
            displayName="Organization URL",
            name="org_url",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p_url.value = "https://www.arcgis.com"

        p_user = arcpy.Parameter(
            displayName="Username (leave blank to use ArcGIS Pro sign-in)",
            name="username",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )

        p_pass = arcpy.Parameter(
            displayName="Password",
            name="password",
            datatype="GPStringHidden",
            parameterType="Optional",
            direction="Input",
        )

        p_out = arcpy.Parameter(
            displayName="Snapshot Save Location",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input",
        )

        p_scope = arcpy.Parameter(
            displayName="Which Services",
            name="scope",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p_scope.filter.type = "ValueList"
        p_scope.filter.list = [SCOPE_MINE, SCOPE_ORG]
        p_scope.value = SCOPE_ORG

        p_fmt = arcpy.Parameter(
            displayName="Export Format",
            name="export_format",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p_fmt.filter.type = "ValueList"
        p_fmt.filter.list = EXPORT_FORMATS
        p_fmt.value = "File Geodatabase"

        p_views = arcpy.Parameter(
            displayName="Include hosted feature layer views",
            name="include_views",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p_views.value = False

        return [p_url, p_user, p_pass, p_out, p_scope, p_fmt, p_views]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        user, pwd = parameters[1], parameters[2]
        if user.valueAsText and not pwd.valueAsText:
            pwd.setErrorMessage("Enter a password, or clear the username to use your ArcGIS Pro sign-in.")
        return

    def execute(self, parameters, messages):
        url = parameters[0].valueAsText.strip().rstrip("/")
        username = parameters[1].valueAsText
        password = parameters[2].valueAsText
        out_root = parameters[3].valueAsText
        scope = parameters[4].valueAsText
        export_format = parameters[5].valueAsText
        include_views = bool(parameters[6].value)

        gis = connect(url, username, password)
        me = gis.users.me
        arcpy.AddMessage("Connected to {} as {}".format(gis.properties.get("name", url), me.username))

        run_folder = make_run_folder(out_root, "HostedFeatureServices")
        data_folder = os.path.join(run_folder, "data")
        meta_folder = os.path.join(run_folder, "metadata")
        os.makedirs(data_folder)
        os.makedirs(meta_folder)
        arcpy.AddMessage("Snapshot folder: {}".format(run_folder))

        # ---- Find hosted feature services
        if scope == SCOPE_MINE:
            query = 'owner:"{}" AND type:"Feature Service"'.format(me.username)
        else:
            query = 'orgid:{} AND type:"Feature Service"'.format(gis.properties.id)

        items = [
            i for i in search_all(gis, query)
            if i.type == "Feature Service" and "Hosted Service" in (i.typeKeywords or [])
        ]
        if not include_views:
            items = [i for i in items if "View Service" not in (i.typeKeywords or [])]
        items.sort(key=lambda i: (i.title or "").lower())

        arcpy.AddMessage("Found {} hosted feature service(s).".format(len(items)))

        results = []  # (item_id, title, owner, status, file)
        arcpy.SetProgressor("step", "Exporting services...", 0, max(len(items), 1), 1)
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        for n, item in enumerate(items, start=1):
            label = "{} ({})".format(item.title, item.id)
            arcpy.SetProgressorLabel("[{}/{}] {}".format(n, len(items), label))
            arcpy.AddMessage("[{}/{}] Exporting {}".format(n, len(items), label))
            export_item = None
            try:
                # Save the item's metadata alongside the data
                with open(os.path.join(meta_folder, "{}_{}.json".format(item.id, safe_name(item.title))),
                          "w", encoding="utf-8") as f:
                    json.dump(dict(item), f, indent=2, default=str)

                export_title = "snapshot_tmp_{}_{}".format(item.id, stamp)
                export_item = item.export(export_title, export_format, wait=True)
                downloaded = export_item.download(save_path=data_folder)
                if not downloaded or not os.path.exists(downloaded):
                    raise RuntimeError("Download returned no file.")

                # Rename to <title>_<itemid>.<ext>
                ext = os.path.splitext(downloaded)[1] or ".zip"
                final = os.path.join(data_folder, "{}_{}{}".format(safe_name(item.title), item.id, ext))
                if os.path.exists(final):
                    os.remove(final)
                os.rename(downloaded, final)

                results.append((item.id, item.title, item.owner, "OK", os.path.basename(final)))
                arcpy.AddMessage("    saved {}".format(os.path.basename(final)))
            except Exception as ex:
                msg = str(ex).splitlines()[0] if str(ex) else ex.__class__.__name__
                results.append((item.id, item.title, item.owner, "FAILED: " + msg, ""))
                arcpy.AddWarning("    FAILED: {}".format(msg))
                arcpy.AddMessage(traceback.format_exc())
            finally:
                if export_item is not None:
                    try:
                        delete_item(export_item)
                    except Exception as ex:
                        arcpy.AddWarning("    Could not delete temporary export item {}: {}".format(
                            export_item.id, ex))
            arcpy.SetProgressorPosition()

        arcpy.ResetProgressor()
        log_path = write_log(run_folder, url, me.username, scope, export_format, results)

        ok = sum(1 for r in results if r[3] == "OK")
        arcpy.AddMessage("Done. {} of {} services downloaded. Log: {}".format(ok, len(results), log_path))
        if ok < len(results):
            arcpy.AddWarning("{} service(s) failed - see the log for details.".format(len(results) - ok))
        return

    def postExecute(self, parameters):
        return


def write_log(run_folder, url, username, scope, export_format, results):
    log_path = os.path.join(run_folder, "download_log.txt")
    ok = [r for r in results if r[3] == "OK"]
    failed = [r for r in results if r[3] != "OK"]
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Hosted Feature Service Snapshot\n")
        f.write("Run date:      {}\n".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        f.write("Organization:  {}\n".format(url))
        f.write("Run by:        {}\n".format(username))
        f.write("Scope:         {}\n".format(scope))
        f.write("Export format: {}\n".format(export_format))
        f.write("Downloaded:    {} of {}\n".format(len(ok), len(results)))
        f.write("=" * 100 + "\n\n")

        f.write("DOWNLOADED\n")
        f.write("{:<34}{:<45}{}\n".format("Item ID", "Item Name", "File"))
        f.write("-" * 100 + "\n")
        for item_id, title, owner, status, fname in ok:
            f.write("{:<34}{:<45}{}\n".format(item_id, title, fname))

        if failed:
            f.write("\nFAILED\n")
            f.write("{:<34}{:<45}{}\n".format("Item ID", "Item Name", "Reason"))
            f.write("-" * 100 + "\n")
            for item_id, title, owner, status, fname in failed:
                f.write("{:<34}{:<45}{} (owner: {})\n".format(item_id, title, status, owner))
    return log_path
