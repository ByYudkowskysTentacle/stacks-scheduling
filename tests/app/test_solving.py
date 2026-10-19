# SPDX-License-Identifier: AGPL-3.0-or-later
"""Milestone 4 in the app: running solves as what-ifs, applying an option as drafts, undo,
locks for published shifts, desired-hours approval and the rules screens. Solves run inline
with short time limits here (test_settings)."""

from __future__ import annotations

from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from app.availability.models import Availability
from app.core.models import Term
from app.people.models import Employee, Tag
from app.scheduling import services, solver
from app.scheduling.models import (
    DesiredHours,
    DesiredStatus,
    DutyAssignment,
    HourTargetRule,
    RunStatus,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    SolveRun,
    TagCoverageRule,
)

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)


@pytest.fixture
def term(org):
    return Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )


@pytest.fixture
def staff(org, term, pay_role):
    people = []
    for i, name in enumerate(["Avery", "Blake", "Casey", "Devon", "Emery", "Finley"], start=1):
        e = Employee.objects.create(
            organization=org,
            full_name=f"{name} Example",
            preferred_name=name,
            email=f"s{i}@example.edu",
            default_pay_role=pay_role,
        )
        Availability.objects.create(organization=org, employee=e, term=term)
        people.append(e)
    return people


@pytest.fixture
def period(org, location, term, staff):
    rules = SchedulingRules.for_org(org)
    rules.coverage_default = 1  # small enough to solve in a few seconds
    rules.freeze_window_hours = 0
    rules.save()
    return SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=1)


def run_solve(client, period, mode, **extra):
    data = {"mode": mode, "change_limit_percent": 30, "freeze_hours": 0, "first_day": "", "last_day": ""}
    data.update(extra)
    with_callbacks = client.post(reverse("solve_start", args=[period.pk]), data)
    assert with_callbacks.status_code == 302
    return SolveRun.objects.order_by("-pk").first()


def test_generate_is_a_what_if_until_applied(as_user, supervisor, period, django_capture_on_commit_callbacks):
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "generate")
    run.refresh_from_db()
    assert run.status == RunStatus.DONE, run.error
    assert not Shift.objects.exists()  # nothing saved by solving
    option = run.result["options"][0]
    assert option["summary"] and option["status_label"]
    page = client.get(reverse("solve_run", args=[period.pk, run.pk])).content.decode()
    assert "Generated schedule" in page and "Apply as draft" in page

    client.post(reverse("solve_run", args=[period.pk, run.pk]), {"action": "apply", "option": "generate"})
    assert not Shift.objects.exists()  # needs confirmation
    response = client.post(
        reverse("solve_run", args=[period.pk, run.pk]),
        {"action": "apply", "option": "generate", "confirm": "yes"},
        follow=True,
    )
    assert "Validation:" in response.content.decode()
    assert Shift.objects.live().count() == len([s for s in option["shifts"] if s["employee_id"]])
    assert all(s.pay_role_id for s in Shift.objects.all())  # default pay role filled in
    assert mail.outbox == []  # applying is a draft change: nobody is notified
    run.refresh_from_db()
    assert run.status == RunStatus.APPLIED


def test_undo_restores_the_working_copy(
    as_user, supervisor, period, staff, django_capture_on_commit_callbacks
):
    manual = Shift.objects.create(
        organization=period.organization,
        location=period.location,
        period=period,
        employee=staff[0],
        date=date(2027, 1, 18),
        start_min=540,
        end_min=720,
    )
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "generate")
    client.post(
        reverse("solve_run", args=[period.pk, run.pk]),
        {"action": "apply", "option": "generate", "confirm": "yes"},
    )
    client.post(reverse("solve_run", args=[period.pk, run.pk]), {"action": "undo", "confirm": "yes"})
    live = list(Shift.objects.live())
    assert [(s.pk, s.employee_id, s.start_min) for s in live] == [(manual.pk, staff[0].pk, 540)]


def test_stale_run_cannot_be_applied(as_user, supervisor, period, staff, django_capture_on_commit_callbacks):
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "generate")
    Shift.objects.create(
        organization=period.organization,
        location=period.location,
        period=period,
        employee=staff[1],
        date=date(2027, 1, 19),
        start_min=540,
        end_min=720,
    )
    response = client.post(
        reverse("solve_run", args=[period.pk, run.pk]),
        {"action": "apply", "option": "generate", "confirm": "yes"},
        follow=True,
    )
    assert "changed after this solve started" in response.content.decode()
    assert Shift.objects.count() == 1


def test_rebalance_keeps_published_shifts_locked(
    as_user, supervisor, period, staff, django_capture_on_commit_callbacks
):
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "generate")
        client.post(
            reverse("solve_run", args=[period.pk, run.pk]),
            {"action": "apply", "option": "generate", "confirm": "yes"},
        )
        services.publish(period, supervisor)
    published = {s.pk: (s.employee_id, s.date, s.start_min, s.end_min) for s in Shift.objects.live()}
    HourTargetRule.objects.create(
        organization=period.organization, employee=staff[5], minutes=20 * 60, priority=3
    )
    with django_capture_on_commit_callbacks(execute=True):
        rb = run_solve(client, period, "rebalance")
    rb.refresh_from_db()
    assert rb.status == RunStatus.DONE, rb.error
    for option in rb.result["options"]:
        rows = {
            s["id"]: (s["employee_id"], date.fromisoformat(s["date"]), s["start"], s["end"])
            for s in option["shifts"]
            if s["id"]
        }
        for pk, values in published.items():
            assert rows.get(pk) == values  # D30: published shifts don't move unless allowed for the run


def test_what_if_reruns_with_one_setting_relaxed(
    as_user, supervisor, period, django_capture_on_commit_callbacks
):
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "rebalance")
        client.post(reverse("what_if", args=[period.pk, run.pk]), {"what_if": "allow_published"})
    newest = SolveRun.objects.order_by("-pk").first()
    assert newest.pk != run.pk and newest.params["allow_published"] is True


def test_relaxing_one_hard_rule_is_a_what_if_for_that_run_only(
    as_user, org, supervisor, period, django_capture_on_commit_callbacks
):
    tag = Tag.objects.create(organization=org, name="New employee")
    TagCoverageRule.objects.create(organization=org, tag=tag, kind="not_solo", hard=True)
    TagCoverageRule.objects.create(organization=org, tag=tag, kind="not_alone", hard=True)
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(client, period, "rebalance")
        client.post(
            reverse("what_if", args=[period.pk, run.pk]),
            {"what_if": "relax_rule", "rule": "A New employee never works alone"},
        )
    newest = SolveRun.objects.order_by("-pk").first()
    assert newest.params["relax_tag_rule"] == "A New employee never works alone"
    ctx, _ = solver.build_request(newest)
    assert {r.describe(): r.hard for r in ctx.rules.tag_rules} == {
        "A New employee never works alone": False,
        "A New employee always has someone without the tag on duty": True,
    }
    assert TagCoverageRule.objects.filter(hard=True).count() == 2  # the saved rules are unchanged
    page = client.get(reverse("solve_run", args=[period.pk, newest.pk])).content.decode()
    assert "is treated as a preference in this run only" in page


def test_solving_is_supervisor_only(as_user, lead_user, student_user, period):
    for user in (lead_user, student_user):
        assert as_user(user).get(reverse("solve_start", args=[period.pk])).status_code == 403


def test_failed_solve_reports_and_changes_nothing(
    as_user, supervisor, period, monkeypatch, django_capture_on_commit_callbacks
):
    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(solver.engine, "run", boom)
    with django_capture_on_commit_callbacks(execute=True):
        run = run_solve(as_user(supervisor), period, "generate")
    run.refresh_from_db()
    assert run.status == RunStatus.FAILED and "unchanged" in run.error
    assert "synthetic" not in run.error  # no internals shown


# --- Duties, targets and desired hours flow into the engine ------------------------------------


def test_rules_screen_adds_duties_and_targets(as_user, org, supervisor, staff, period):
    client = as_user(supervisor)
    client.post(
        reverse("rules"),
        {
            "form": "duty",
            "employee": staff[0].pk,
            "kind": "opener",
            "weekdays": [0, 1, 2, 3, 4],
            "short_weekdays": [0, 2, 4],
            "short_minutes": "1.5",
            "weeks": [],
            "minutes": "",
            "off_week_max_minutes": "",
            "label": "lead week",
        },
    )
    client.post(
        reverse("rules"),
        {"form": "target", "employee": staff[1].pk, "minutes": "20", "priority": 3, "note": ""},
    )
    duty = DutyAssignment.objects.get()
    assert duty.short_minutes == 90 and duty.weekdays == [0, 1, 2, 3, 4]
    rules = services.domain_rules(org)
    assert rules.openers[0].employee_id == staff[0].pk and rules.targets[0].minutes == 1200


def test_desired_hours_count_only_once_approved(as_user, org, supervisor, student_user, employee, term):
    client = as_user(student_user)
    client.post(reverse("desired_submit"), {"term": term.pk, "hours": "12"})
    desired = DesiredHours.objects.get()
    assert desired.status == DesiredStatus.PENDING and desired.minutes == 720
    assert services.domain_rules(org, term).desired == []  # pending is ignored (scenario 21)
    as_user(supervisor).post(reverse("desired_hours"), {"pk": desired.pk, "decision": "approved"})
    desired.refresh_from_db()
    assert desired.status == DesiredStatus.APPROVED and desired.decided_by == supervisor
    assert [d.minutes for d in services.domain_rules(org, term).desired] == [720]
    # Changing the request sends it back for approval.
    as_user(student_user).post(reverse("desired_submit"), {"term": term.pk, "hours": "15"})
    desired.refresh_from_db()
    assert desired.status == DesiredStatus.PENDING


def test_students_cannot_approve_desired_hours(as_user, student_user):
    assert as_user(student_user).get(reverse("desired_hours")).status_code == 403
