from enum import Enum


class CollectionName(str, Enum):
    SALES_PLAYBOOKS = "sales_playbooks"
    BANT = "bant"
    EMAIL_TEMPLATES = "email_templates"
    PROPOSAL_TEMPLATES = "proposal_templates"
    PRODUCT_CATALOG = "product_catalog"
    SERVICE_CATALOG = "service_catalog"
    INDUSTRY_EXAMPLES = "industry_examples"
