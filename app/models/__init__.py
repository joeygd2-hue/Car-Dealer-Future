"""Import every model so SQLAlchemy metadata (and Alembic autogenerate) sees the full schema."""

from app.models.appointments import Appointment, AppointmentEvent
from app.models.automation import (
    FAQ,
    MODE_CONTROLLED,
    MODE_DRAFT_ONLY,
    MODE_FAQ,
    MODES,
    ApprovedTemplate,
    AutomationSettings,
    EvaluationRun,
    ModeAuthorization,
)
from app.models.conversations import Conversation, Escalation, InboundEvent, Message, ResponseDraft
from app.models.core import (
    AuditLog,
    BusinessHours,
    Dealership,
    IntegrationConnection,
    LiveAuthorization,
    Membership,
    SystemControl,
    User,
    UserSession,
)
from app.models.crm import ContactSuppression, CustomerContact, CustomerIdentity, CustomerLead
from app.models.inventory import ListingDraft, Vehicle
from app.models.jobs import Job, RecurringTask, WorkerHeartbeat
from app.models.sandbox import SandboxRecord
from app.models.notifications import Notification, NotificationPreference
from app.models.outreach import (
    Campaign,
    CostEntry,
    Enrollment,
    OutreachAgentState,
    OutreachFAQ,
    OutreachMessage,
    OutreachReply,
    Prospect,
    ProspectFact,
    SalesMeeting,
    Suppression,
)

__all__ = [
    "FAQ", "MODES", "MODE_CONTROLLED", "MODE_DRAFT_ONLY", "MODE_FAQ",
    "Appointment", "AppointmentEvent", "ApprovedTemplate", "AuditLog", "AutomationSettings",
    "BusinessHours", "Campaign", "ContactSuppression", "Conversation", "CostEntry", "CustomerContact",
    "CustomerIdentity", "CustomerLead", "Dealership", "Enrollment", "Escalation", "EvaluationRun",
    "InboundEvent", "IntegrationConnection", "Job", "ListingDraft", "LiveAuthorization", "Membership",
    "Message", "ModeAuthorization", "Notification", "NotificationPreference", "OutreachAgentState", "OutreachFAQ",
    "OutreachMessage", "OutreachReply", "Prospect", "ProspectFact", "RecurringTask", "ResponseDraft",
    "SalesMeeting", "SandboxRecord", "Suppression", "SystemControl", "User", "UserSession", "Vehicle", "WorkerHeartbeat",
]
