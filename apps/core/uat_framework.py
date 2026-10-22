"""Shared machinery for role-agent UATs (used by the uat_roles command)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.test import Client
from django.urls import reverse

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN, MAGENTA = "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[35m"
D = Decimal


class _Undo(Exception):
    """Raised to roll a probe's savepoint back after it has been measured."""


@dataclass
class Outcome:
    kind: str            # "guarded", "silent", "accepted", "crashed"
    detail: str = ""


@dataclass
class ProbeResult:
    company: str
    module: str
    role: str
    name: str
    risk: str
    severity: str        # how bad it is if the app ACCEPTS it
    outcome: Outcome


@dataclass
class StepResult:
    company: str
    module: str
    role: str
    name: str
    ok: bool
    detail: str = ""


def feedback(response) -> list[str]:
    """Messages and form errors the person would see after this request."""
    out = []
    req = getattr(response, "wsgi_request", None)
    storage = getattr(req, "_messages", None)
    for m in getattr(storage, "_queued_messages", []) or []:
        out.append(str(m))
    ctx = getattr(response, "context", None)
    if ctx:
        for key in ("form", "line_formset", "formset"):
            try:
                f = ctx[key]
            except (KeyError, TypeError):
                continue
            errs = getattr(f, "errors", None)
            if isinstance(errs, dict):
                for field_name, e in errs.items():
                    out.append(f"{field_name}: {' '.join(e)}")
            elif isinstance(errs, list):
                for e in errs:
                    if e:
                        out.append(str(e))
            nfe = getattr(f, "non_form_errors", None)
            if callable(nfe):
                out.extend(str(e) for e in nfe())
    if getattr(response, "status_code", 200) == 403:
        out.append("no access")
    return out


@dataclass
class Agent:
    """One employee, signed in through the real login page."""
    company: object
    role: str
    user: object
    password: str
    name: str
    client: Client = None
    last_response: object = None

    def login(self):
        c = Client(enforce_csrf_checks=True)
        c.get(reverse("accounts:login"))
        r = c.post(reverse("accounts:login"), {"email": self.user.email, "password": self.password,
                                               "csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        self.client = c
        return r.status_code == 302

    def token(self):
        if "csrftoken" not in self.client.cookies:
            self.client.get(reverse("dashboard:index"))
        return self.client.cookies["csrftoken"].value

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["csrfmiddlewaretoken"] = self.token()
        self.last_response = self.client.post(url, data, **kw)
        return self.last_response

    def post_json(self, url, payload):
        import json
        self.last_response = self.client.post(url, json.dumps(payload), content_type="application/json",
                                              HTTP_X_CSRFTOKEN=self.token())
        return self.last_response

    def get(self, url):
        self.last_response = self.client.get(url)
        return self.last_response


class Harness:
    """Runs module steps and bad-transaction probes, and keeps score."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.probes: list[ProbeResult] = []
        self.steps: list[StepResult] = []
        self.tag = secrets.token_hex(3).upper()

    # ---- legitimate work (module UAT) --------------------------------
    def step(self, company, module, agent, name, fn):
        try:
            with transaction.atomic():
                ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"crashed: {type(exc).__name__}: {str(exc)[:160]}"
        self.steps.append(StepResult(company.name, module, agent.role if agent else "-", name, ok, detail))
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        self.stdout.write(f"    {mark} {name}{DIM}{'  — ' + detail if detail and not ok else ''}{RESET}")
        return ok

    # ---- bad transactions --------------------------------------------
    def probe(self, company, module, agent, name, risk, severity, attempt):
        """`attempt()` performs the bad transaction and returns (accepted, detail).

        Everything it does is rolled back afterwards, so an accepted bad
        transaction can't affect later tests.
        """
        holder = {}
        try:
            with transaction.atomic():
                accepted, detail = attempt()
                holder["accepted"], holder["detail"] = accepted, detail
                holder["feedback"] = feedback(agent.last_response) if agent and agent.last_response is not None else []
                raise _Undo()
        except _Undo:
            pass
        except Exception as exc:
            holder = {"crash": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if "crash" in holder:
            outcome = Outcome("crashed", holder["crash"])
        elif holder["accepted"]:
            outcome = Outcome("accepted", holder["detail"])
        elif holder["feedback"]:
            outcome = Outcome("guarded", holder["feedback"][0][:140])
        else:
            outcome = Outcome("silent", holder["detail"] or "refused without telling the user why")
        self.probes.append(ProbeResult(company.name, module, agent.role if agent else "-", name, risk,
                                       severity, outcome))
        colour = {"guarded": GREEN, "silent": YELLOW, "accepted": RED, "crashed": MAGENTA}[outcome.kind]
        label = {"guarded": "guarded ", "silent": "silent  ", "accepted": "ACCEPTED", "crashed": "CRASHED "}[outcome.kind]
        self.stdout.write(f"    {colour}{label}{RESET} {name}{DIM}  — {outcome.detail}{RESET}")
        return outcome
