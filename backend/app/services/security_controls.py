from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.security_setting import SecuritySetting


def registration_enabled(db: Session) -> bool:
    saved = db.query(SecuritySetting).filter(SecuritySetting.id == 1).first()
    return bool(saved.registration_enabled) if saved is not None else bool(settings.REGISTRATION_ENABLED)


def set_registration_enabled(db: Session, enabled: bool) -> bool:
    saved = db.query(SecuritySetting).filter(SecuritySetting.id == 1).first()
    if saved is None:
        saved = SecuritySetting(id=1, registration_enabled=enabled)
        db.add(saved)
    else:
        saved.registration_enabled = enabled
    db.commit()
    return bool(enabled)
