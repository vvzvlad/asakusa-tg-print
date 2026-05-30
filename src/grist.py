import httpx
from loguru import logger

from src.settings import settings

GLAZE_TABLE = "For_print"
MAKERS_TABLE = "GlazeMakers"


class GristError(Exception):
    """Raised when the Grist API request fails."""


class GristClient:
    """Reads glaze rows from the Grist `For_print` table over the REST API.

    The label's maker link comes from the `Maker` reference column, resolved
    against the `GlazeMakers` table (i.e. the `$Maker.Makerlink` formula).
    """

    def __init__(self) -> None:
        self.base_url = settings.grist_base_url.rstrip("/")
        self.doc_id = settings.grist_doc_id
        self.api_key = settings.grist_api_key
        self.timeout = settings.label_maker_timeout

    async def _records(self, table: str) -> list[dict]:
        if not self.api_key:
            raise GristError("GRIST_API_KEY is not configured")
        url = f"{self.base_url}/api/docs/{self.doc_id}/tables/{table}/records"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise GristError(f"Grist unreachable: {e}") from e
        if resp.status_code != 200:
            raise GristError(f"Grist returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "records" not in data:
            raise GristError(f"Unexpected Grist response: {str(data)[:200]}")
        return data["records"]

    async def find_glaze(self, query: str) -> dict | None:
        """Find the first For_print row whose GeneralName matches `query`.

        Prefers a case-insensitive exact match, otherwise the first
        case-insensitive substring match in table order. The returned dict is
        the row's `fields` plus a resolved `Makerlink` key (from the Maker ref).
        Returns None if nothing matches.
        """
        needle = query.strip().lower()
        records = await self._records(GLAZE_TABLE)

        def name(rec: dict) -> str:
            return (rec["fields"].get("GeneralName") or "").strip()

        match = next((r for r in records if name(r).lower() == needle), None)
        if match is None:
            match = next((r for r in records if needle in name(r).lower()), None)
        if match is None:
            return None
        if name(match).lower() != needle:
            logger.info("Glaze search {!r} -> closest match {!r}", query, name(match))

        fields = dict(match["fields"])
        fields["Makerlink"] = await self._maker_link(fields.get("Maker"))
        return fields

    async def _maker_link(self, maker_ref) -> str:
        """Resolve a Maker reference (row id) to its Makerlink in GlazeMakers."""
        maker_id = _ref_id(maker_ref)
        if maker_id is None:
            return ""
        makers = {r["id"]: r["fields"] for r in await self._records(MAKERS_TABLE)}
        return str(makers.get(maker_id, {}).get("Makerlink") or "")


def _ref_id(value) -> int | None:
    """Extract the target row id from a Grist Reference cell value.

    A single reference is returned as a bare row id (int); the tagged form
    ["R", "<Table>", <rowId>] is also handled defensively.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list) and len(value) >= 3 and value[0] == "R":
        return value[2]
    return None


def glaze_entities(fields: dict) -> list[str]:
    """Build the %E1%..%E8% entity values for the glaze label template.

    Mirrors the per-field formulas requested for the glaze label, with the
    site base URL ($SITE = settings.glaze_site_url):
      1 " ".join($GeneralName.split(" ")[1:])
      2 $GeneralName.split(" ")[0]
      3 $SITE + "/glaze/" + $GeneralName.split(" ")[0]
      4 $Maker.Makerlink
      5 $SITE + "/glaze/icon/GLS" if $Surface == "Глянцевая" else ".../MATT"
      6 $SITE + "/glaze/icon/FG"  if $Foodgrade == "Пищевая"  else ".../NFG"
      7 $Type
      8 $Description
    """
    site = settings.glaze_site_url.rstrip("/")
    general = (fields.get("GeneralName") or "").strip()
    parts = general.split(" ")
    first = parts[0] if parts else ""
    rest = " ".join(parts[1:])
    surface_icon = "GLS" if fields.get("Surface") == "Глянцевая" else "MATT"
    food_icon = "FG" if fields.get("Foodgrade") == "Пищевая" else "NFG"
    return [
        rest,                                       # %E1%
        first,                                      # %E2%
        f"{site}/glaze/{first}",                    # %E3%
        str(fields.get("Makerlink") or ""),         # %E4%
        f"{site}/glaze/icon/{surface_icon}",        # %E5%
        f"{site}/glaze/icon/{food_icon}",           # %E6%
        str(fields.get("Type") or ""),              # %E7%
        str(fields.get("Description") or ""),       # %E8%
    ]
