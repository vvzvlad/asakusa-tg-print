from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    telegram_api_server: str | None = None
    # Comma-separated Telegram user IDs allowed to print; empty = nobody (deny all)
    allowed_user_ids: str = ""
    # Comma-separated Telegram chat IDs (groups) where everyone may print
    allowed_chat_ids: str = ""

    # Label Maker HTTP service (POST /api/generate-pdf). Own (self-deployed)
    # service — its address depends on the deployment, so no default: unset → fail.
    label_maker_url: str
    label_maker_timeout: int = 60
    labels_db_path: str = "data/labels.db"

    # Grist (source for /printglaze) — own service + credential, all required.
    # No defaults: a placeholder/empty value would silently break /printglaze.
    grist_base_url: str
    grist_doc_id: str
    grist_api_key: str
    # Public glaze site base URL printed into the label (QR + icon links). This
    # is our own published site, so its address must come from the environment.
    glaze_site_url: str
    # Rotate the PDF page 90°. False = landscape 58×40mm (direct PDF print to CUPS).
    label_rotate: bool = False

    # CUPS print server host (printed via the `lp` client over IPP). Own service
    # — address is deployment-specific, no default.
    cups_host: str
    # Printer device config (not secrets / not addresses) — sane defaults are ok.
    printer_name: str = "XP-365B"
    printer_media: str = "Custom.165x114pt"
    printer_fit_to_page: bool = True

    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        return {int(p.strip()) for p in raw.split(",") if p.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return self._parse_ids(self.allowed_user_ids)

    @property
    def allowed_chats(self) -> set[int]:
        return self._parse_ids(self.allowed_chat_ids)


settings = Settings()
