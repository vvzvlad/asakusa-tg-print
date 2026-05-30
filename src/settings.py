from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    telegram_api_server: str | None = None
    # Comma-separated Telegram user IDs allowed to print; empty = nobody (deny all)
    allowed_user_ids: str = ""
    # Comma-separated Telegram chat IDs (groups) where everyone may print
    allowed_chat_ids: str = ""

    # Label Maker HTTP service (POST /api/generate-pdf)
    label_maker_url: str = "http://localhost:8000"
    label_maker_timeout: int = 60
    template_path: str = "data/label_template.json"
    glaze_template_path: str = "data/label_template_glaze.json"
    labels_db_path: str = "data/labels.db"

    # Grist (source for /printglaze) — configured via .env
    grist_base_url: str = ""
    grist_doc_id: str = ""
    grist_api_key: str = ""
    # Public glaze site base URL printed into the label (QR + icon links)
    glaze_site_url: str = ""
    # Rotate the PDF page 90°. False = landscape 58×40mm (direct PDF print to CUPS).
    label_rotate: bool = False

    # CUPS / printer (printed via the `lp` client over IPP)
    cups_host: str = "10.31.50.63"
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
