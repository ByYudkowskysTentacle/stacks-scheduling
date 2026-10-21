# SPDX-License-Identifier: AGPL-3.0-or-later
"""Milestone 2: availability entry, status, the availability grid and CSV imports."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pghistory.models import Events

from app.availability.models import Availability, AvailabilityBlock, Status
from app.availability.services import representative_week
from app.core.models import Term
from app.imports.models import BatchStatus, ImportBatch
from app.people.models import Employee, EmployeeTag, Tag
from domain.calendar import MON, SAT

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imports"


@pytest.fixture
def term(org):
    return Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )


def form_data(term, **days):
    data = {"term": term.pk}
    for wd in range(7):
        data[f"unavailable_{wd}"] = ""
        data[f"preferred_{wd}"] = ""
    data.update(days)
    return data


# --- Entry ----------------------------------------------------------------------------------


def test_student_enters_own_availability(as_user, org, term, location, student_user, employee):
    client = as_user(student_user)
    assert client.get(reverse("my_availability")).status_code == 200
    response = client.post(
        reverse("my_availability") + f"?term={term.pk}",
        form_data(
            term, unavailable_0="09:00-09:50, 13:00-14:15", unavailable_5="all day", preferred_2="1pm-5pm"
        ),
    )
    assert response.status_code == 302
    a = Availability.objects.get(employee=employee, term=term)
    assert a.status == Status.ENTERED and a.confirmed_by == student_user
    blocks = sorted((b.weekday, b.kind, b.start_min, b.end_min) for b in a.blocks.all())
    assert blocks == [
        (0, "unavailable", 540, 590),
        (0, "unavailable", 780, 855),
        (2, "preferred", 780, 1020),
        (5, "unavailable", 0, 1440),
    ]
    # Exact off-grid times survive a round trip through the form (SPEC §3.3).
    page = client.get(reverse("my_availability") + f"?term={term.pk}").content.decode()
    assert 'value="09:00-09:50, 13:00-14:15"' in page and 'value="all day"' in page


def test_saving_empty_means_available_any_time_not_not_entered(
    as_user, term, location, student_user, employee
):
    as_user(student_user).post(reverse("my_availability"), form_data(term))
    a = Availability.objects.get(employee=employee, term=term)
    assert a.status == Status.ENTERED and a.blocks.count() == 0


def test_bad_ranges_show_errors_and_save_nothing(as_user, term, location, student_user, employee):
    response = as_user(student_user).post(reverse("my_availability"), form_data(term, unavailable_0="9-8"))
    assert response.status_code == 200
    assert "ends before it starts" in response.content.decode()
    assert not Availability.objects.filter(employee=employee).exists()


def test_supervisor_edits_on_behalf_and_it_is_audited(as_user, term, location, supervisor, employee):
    url = reverse("employee_availability", args=[employee.pk]) + f"?term={term.pk}"
    assert as_user(supervisor).post(url, form_data(term, unavailable_1="10:00-11:00")).status_code == 302
    block = AvailabilityBlock.objects.get(availability__employee=employee)
    event = Events.objects.filter(pgh_obj_model="availability.AvailabilityBlock").order_by("-pgh_id").first()
    assert event.pgh_context["user"] == supervisor.pk and block.start_min == 600


def test_students_cannot_edit_someone_elses_availability(as_user, org, term, student_user):
    other = Employee.objects.create(organization=org, full_name="Blake Sample", preferred_name="Blake")
    client = as_user(student_user)
    assert client.get(reverse("employee_availability", args=[other.pk])).status_code == 403


def test_term_from_another_organization_is_refused(as_user, other_org, student_user, location):
    foreign = Term.objects.create(
        organization=other_org, name="X", start_date=date(2027, 1, 1), end_date=date(2027, 5, 1)
    )
    assert as_user(student_user).get(reverse("my_availability") + f"?term={foreign.pk}").status_code == 404


def test_confirm_needs_review(as_user, org, term, student_user, employee):
    a = Availability.objects.create(
        organization=org, employee=employee, term=term, status=Status.NEEDS_REVIEW
    )
    as_user(student_user).post(
        reverse("availability_confirm", args=[a.pk]), {"next": "https://evil.example.com/"}
    )
    a.refresh_from_db()
    assert a.status == Status.ENTERED


def test_cannot_confirm_someone_elses_availability(as_user, org, term, student_user):
    other = Employee.objects.create(organization=org, full_name="Blake Sample", preferred_name="Blake")
    a = Availability.objects.create(organization=org, employee=other, term=term, status=Status.NEEDS_REVIEW)
    assert as_user(student_user).post(reverse("availability_confirm", args=[a.pk])).status_code == 404
    a.refresh_from_db()
    assert a.status == Status.NEEDS_REVIEW


def test_representative_week_is_inside_the_term(org, term):
    # Today outside the term: show the term's first full Saturday-to-Friday week.
    assert representative_week(org, term, date(2026, 10, 19)) == date(2027, 1, 16)
    assert representative_week(org, term, date(2027, 2, 3)) == date(2027, 1, 30)


# --- Grid -----------------------------------------------------------------------------------


@pytest.fixture
def grid_people(org, term, employee):
    tag = Tag.objects.create(organization=org, name="Cataloguer")
    EmployeeTag.objects.create(organization=org, employee=employee, tag=tag)
    a = Availability.objects.create(organization=org, employee=employee, term=term)
    AvailabilityBlock.objects.create(
        organization=org, availability=a, weekday=MON, kind="unavailable", start_min=540, end_min=590
    )
    AvailabilityBlock.objects.create(
        organization=org, availability=a, weekday=SAT, kind="unavailable", start_min=0, end_min=1440
    )
    Employee.objects.create(organization=org, full_name="Casey Placeholder", preferred_name="Casey")
    return tag


def test_grid_shows_hours_status_and_tags_to_supervisors(as_user, term, location, supervisor, grid_people):
    page = as_user(supervisor).get(reverse("availability_grid") + f"?term={term.pk}").content.decode()
    assert "68.5 of 73.5 h" in page  # 73.5 minus Saturday's 4 hours minus Monday 9-10
    assert "Out 9am–9:50am" in page
    assert "Not entered" in page  # Casey, flagged rather than treated as free
    assert "Cataloguer" in page


def test_shift_leads_see_free_busy_but_not_tags(as_user, term, location, lead_user, grid_people):
    response = as_user(lead_user).get(reverse("availability_grid") + f"?term={term.pk}")
    assert response.status_code == 200
    page = response.content.decode()
    assert "Avery" in page and "Cataloguer" not in page


def test_students_cannot_see_the_grid(as_user, student_user, term):
    assert as_user(student_user).get(reverse("availability_grid")).status_code == 403


def test_grid_filters_by_status(as_user, term, location, supervisor, grid_people):
    page = (
        as_user(supervisor)
        .get(reverse("availability_grid") + f"?term={term.pk}&status=not_entered")
        .content.decode()
    )
    assert "Casey" in page and "Avery" not in page


def test_nav_shows_links_for_the_role(as_user, supervisor, student_user, term, location):
    page = as_user(supervisor).get(reverse("home")).content.decode()
    assert reverse("availability_grid") in page and f'href="{reverse("organization_settings")}"' in page
    page = as_user(student_user).get(reverse("home")).content.decode()
    assert reverse("my_availability") in page and f'href="{reverse("organization_settings")}"' not in page


# --- Imports --------------------------------------------------------------------------------


def upload(client, kind, filename, content, term=None):
    data = {"kind": kind, "file": SimpleUploadedFile(filename, content.encode(), content_type="text/csv")}
    if term:
        data["term"] = term.pk
    return client.post(reverse("imports"), data)


def test_roster_import_preview_apply_and_undo(as_user, org, supervisor, pay_role):
    client = as_user(supervisor)
    existing = Employee.objects.create(
        organization=org, full_name="Avery Old", preferred_name="Avery", email="student01@example.edu"
    )
    response = upload(client, "roster", "roster.csv", (FIXTURES / "roster.csv").read_text())
    batch = ImportBatch.objects.get()
    assert response.status_code == 302 and batch.status == BatchStatus.PREVIEW
    assert len(batch.plan["create"]) == 3 and len(batch.plan["update"]) == 1
    assert Employee.objects.count() == 1  # nothing changes before apply

    client.post(reverse("import_detail", args=[batch.pk]), {"action": "apply"})
    batch.refresh_from_db()
    assert batch.status == BatchStatus.APPLIED
    assert Employee.objects.filter(organization=org).count() == 4
    existing.refresh_from_db()
    assert existing.full_name == "Avery Example"
    assert [t.name for t in existing.tags_on(date(2027, 1, 1))] == ["Opener"]
    blake = Employee.objects.get(email="student02@example.edu")
    assert {t.name for t in blake.tags_on(date(2027, 1, 1))} == {"Shift lead", "Cataloguer"}
    assert blake.default_pay_role.name == "FWS Student Assistant"
    # The batch groups every row change under one audit context.
    assert (
        Events.objects.filter(pgh_context__import_batch=batch.pk, pgh_obj_model="people.Employee").count()
        == 4
    )

    client.post(reverse("import_detail", args=[batch.pk]), {"action": "undo"})  # no confirmation
    assert Employee.objects.filter(organization=org).count() == 4
    client.post(reverse("import_detail", args=[batch.pk]), {"action": "undo", "confirm": "yes"})
    batch.refresh_from_db()
    assert batch.status == BatchStatus.UNDONE
    assert list(Employee.objects.filter(organization=org)) == [existing]
    existing.refresh_from_db()
    assert existing.full_name == "Avery Old" and existing.employee_tags.count() == 0
    assert not Tag.objects.filter(name="Shift lead").exists()


def test_roster_with_problems_cannot_be_applied(as_user, org, supervisor):
    client = as_user(supervisor)
    upload(client, "roster", "bad.csv", "full_name,preferred_name,email\nA B,,a@example.edu\n")
    batch = ImportBatch.objects.get()
    assert batch.plan["problems"] == ["row 2, preferred_name: required"]
    client.post(reverse("import_detail", args=[batch.pk]), {"action": "apply"})
    batch.refresh_from_db()
    assert batch.status == BatchStatus.PREVIEW and not Employee.objects.exists()


def test_roster_import_never_removes_tags_or_blanks_fields(as_user, org, supervisor):
    emp = Employee.objects.create(
        organization=org,
        full_name="Avery Example",
        preferred_name="Avery",
        email="a@example.edu",
        campus_id="X1",
    )
    tag = Tag.objects.create(organization=org, name="Opener")
    EmployeeTag.objects.create(organization=org, employee=emp, tag=tag)
    client = as_user(supervisor)
    upload(
        client,
        "roster",
        "r.csv",
        "full_name,preferred_name,email,campus_id,tags\nAvery Example,Avery,a@example.edu,,\n",
    )
    batch = ImportBatch.objects.get()
    assert batch.plan["unchanged"] == 1
    client.post(reverse("import_detail", args=[batch.pk]), {"action": "apply"})
    emp.refresh_from_db()
    assert emp.campus_id == "X1" and emp.employee_tags.count() == 1


def test_availability_import_marks_needs_review_and_undo_restores(as_user, org, supervisor, term, location):
    client = as_user(supervisor)
    for i in (1, 2, 3):
        Employee.objects.create(
            organization=org, full_name=f"P{i}", preferred_name=f"P{i}", email=f"student0{i}@example.edu"
        )
    first = Employee.objects.get(email="student01@example.edu")
    prior = Availability.objects.create(organization=org, employee=first, term=term, status=Status.ENTERED)
    AvailabilityBlock.objects.create(
        organization=org, availability=prior, weekday=MON, kind="unavailable", start_min=600, end_min=660
    )

    upload(client, "availability", "a.csv", (FIXTURES / "availability.csv").read_text(), term=term)
    batch = ImportBatch.objects.get()
    assert not batch.plan["problems"], batch.plan["problems"]
    client.post(reverse("import_detail", args=[batch.pk]), {"action": "apply"})
    prior.refresh_from_db()
    assert prior.status == Status.NEEDS_REVIEW
    assert sorted((b.weekday, b.start_min, b.end_min) for b in prior.blocks.all())[0] == (0, 540, 590)
    assert Availability.objects.filter(term=term).count() == 3

    client.post(reverse("import_detail", args=[batch.pk]), {"action": "undo", "confirm": "yes"})
    prior.refresh_from_db()
    assert prior.status == Status.ENTERED
    assert [(b.weekday, b.start_min) for b in prior.blocks.all()] == [(MON, 600)]
    assert Availability.objects.filter(term=term).count() == 1


def test_availability_import_rejects_unknown_people(as_user, supervisor, term):
    upload(
        as_user(supervisor),
        "availability",
        "a.csv",
        "email,weekday,type\nnobody@example.edu,Mon,unavailable\n",
        term=term,
    )
    assert "no current employee" in ImportBatch.objects.get().plan["problems"][0]


def test_availability_import_needs_a_term(as_user, supervisor):
    response = upload(as_user(supervisor), "availability", "a.csv", "email,weekday,type\n")
    assert response.status_code == 200 and not ImportBatch.objects.exists()


def test_only_the_latest_import_can_be_undone(as_user, org, supervisor):
    client = as_user(supervisor)
    for n in (1, 2):
        upload(
            client, "roster", f"r{n}.csv", f"full_name,preferred_name,email\nP {n},P{n},p{n}@example.edu\n"
        )
        batch = ImportBatch.objects.order_by("-pk").first()
        client.post(reverse("import_detail", args=[batch.pk]), {"action": "apply"})
    older = ImportBatch.objects.order_by("pk").first()
    client.post(reverse("import_detail", args=[older.pk]), {"action": "undo", "confirm": "yes"})
    older.refresh_from_db()
    assert older.status == BatchStatus.APPLIED


def test_imports_are_supervisor_only_and_org_scoped(as_user, student_user, supervisor, other_org):
    assert as_user(student_user).get(reverse("imports")).status_code == 403
    foreign = ImportBatch.objects.create(organization=other_org, kind="roster")
    assert as_user(supervisor).get(reverse("import_detail", args=[foreign.pk])).status_code == 404


def test_windows_encoded_file_is_accepted(as_user, supervisor):
    content = "full_name,preferred_name,email\nRenée Sample,Renée,r@example.edu\n".encode("cp1252")
    as_user(supervisor).post(
        reverse("imports"),
        {"kind": "roster", "file": SimpleUploadedFile("r.csv", content, content_type="text/csv")},
    )
    assert ImportBatch.objects.get().plan["create"][0]["name"] == "Renée"
