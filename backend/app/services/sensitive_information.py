"""Conservative request flag; not clinical reasoning or a compliance claim."""
import re


def sensitive_health_request(message):
    return bool(re.search(r"\b(patient|diagnos\w*|medication|prescription|medical office|hipaa|"
                          r"medical record|health record|symptoms?|treatment plan|insurance claim)\b", message or "", re.I))
