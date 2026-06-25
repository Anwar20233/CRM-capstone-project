import os
import csv
import re
import pytest
from unittest.mock import MagicMock

# Force the import of the module under test
import pipelines.ner_pipeline as ner

@pytest.fixture(autouse=True)
def mock_gliner():
    # Mock GLiNER models so we don't load 1.3GB of weights during unit tests
    orig_lg = ner._model_lg
    orig_md = ner._model_md
    
    mock_model = MagicMock()
    mock_model.predict_entities.return_value = []
    
    ner._model_lg = mock_model
    ner._model_md = mock_model
    
    yield
    
    ner._model_lg = orig_lg
    ner._model_md = orig_md


def test_ner_reliability_on_gold_standard():
    # 1. Load the gold-standard CSV dataset
    csv_path = os.path.join(os.path.dirname(__file__), "data", "customers-1000.csv")
    assert os.path.exists(csv_path), f"Gold standard dataset not found at {csv_path}"

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 2. Track metrics
    total_emails = 0
    detected_emails = 0
    
    total_phones = 0
    detected_phones = 0
    
    false_positive_ids = 0

    # Test a subset or all records. Let's run on 100 records for fast test execution, 
    # or all of them. Since the regex/rule-based execution is super fast, let's run all 1000!
    for i, row in enumerate(rows):
        email = row.get("Email", "").strip()
        phone1 = row.get("Phone1", "").strip()
        phone2 = row.get("Phone2", "").strip()
        cust_id = row.get("CustomerId", "").strip()
        
        # Test 1: Test raw values directly
        if email:
            total_emails += 1
            ents = ner.extract_emails(email)
            if any(e["text"].lower() == email.lower() for e in ents):
                detected_emails += 1

        if phone1:
            total_phones += 1
            ents = ner.extract_phones(phone1)
            # Match could be a substring or exact
            if any(p["text"] in phone1 or phone1 in p["text"] for p in ents):
                detected_phones += 1

        if phone2:
            total_phones += 1
            ents = ner.extract_phones(phone2)
            if any(p["text"] in phone2 or phone2 in p["text"] for p in ents):
                detected_phones += 1

        # Test 2: CustomerId should not be parsed as phone
        if cust_id:
            ents = ner.extract_phones(cust_id)
            if ents:
                false_positive_ids += 1

        # Test 3: Contextual sentence extraction
        # We construct a realistic CRM note containing the customer's PII and ID
        note = (
            f"Logged ticket for customer {cust_id}. Contact: {row['FirstName']} {row['LastName']} "
            f"from {row['Company']}. Direct phone: {phone1}. Alternative: {phone2}. "
            f"Email address: {email}. Website: {row['Website']}."
        )
        
        # Run full extract pipeline (excluding GLiNER since it is mocked to return [])
        extracted = ner.extract(note)
        
        # Verify that customer ID is not extracted as a phone number or email
        extracted_phones = [e for e in extracted if e["label"] == "phone number"]
        extracted_emails = [e for e in extracted if e["label"] == "email address"]
        
        for ep in extracted_phones:
            if ep["text"] == cust_id:
                false_positive_ids += 1
                
        for em in extracted_emails:
            if em["text"] == cust_id:
                false_positive_ids += 1

    # Print summary metrics for readability
    email_recall = detected_emails / total_emails if total_emails > 0 else 1.0
    phone_recall = detected_phones / total_phones if total_phones > 0 else 1.0
    
    print(f"\n--- Evaluation Metrics on 1000 synthetic customers ---")
    print(f"Emails: {detected_emails}/{total_emails} ({email_recall:.2%})")
    print(f"Phones: {detected_phones}/{total_phones} ({phone_recall:.2%})")
    print(f"CustomerId False Positives: {false_positive_ids}")

    # Assertions
    assert email_recall >= 0.98, f"Email recall too low: {email_recall:.2%}"
    assert phone_recall >= 0.95, f"Phone recall too low: {phone_recall:.2%}"
    assert false_positive_ids == 0, f"Customer ID false positives detected: {false_positive_ids}"


def test_pii_reliability_traps():
    # Trap values from proof_phone_guard.py and PII_MASKING_RELIABILITY_PLAN.md
    traps = [
        "ccc3198c-eeb1-43f3-849f-fc72aeffb0a2",
        "company id 711b5b56-3fdf-4d54-8454-737adbab2e65",
        "order 1234567890",
        "SKU 99-8454-737",
        "invoice ref 4500123456",
        "version 1.0.0-beta.2",
    ]
    
    for t in traps:
        ents = ner.extract_phones(t)
        assert not ents, f"Trap value '{t}' incorrectly matched as phone number: {ents}"
