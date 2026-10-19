# SPDX-License-Identifier: AGPL-3.0-or-later
"""Invite an employee to sign in: link (or create) their account, give them the employee
role, and email a one-time link to set a password. Supervisor-triggered only, never a side
effect of editing (CLAUDE.md §2.2)."""

from __future__ import annotations

from allauth.account.forms import default_token_generator
from allauth.account.internal.flows.password_reset import get_reset_password_from_key_url
from allauth.account.utils import user_pk_to_url_str
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string

from app.accounts.models import User
from app.core.models import Membership, Role
from app.people.models import Employee


class InviteError(Exception):
    pass


def invite(request, employee: Employee) -> User:
    if not employee.email:
        raise InviteError("Add an email address before inviting.")
    org = employee.organization
    user = employee.user or User.objects.filter(email__iexact=employee.email).first()
    if user is None:
        user = User.objects.create_user(employee.email)
    other = Employee.objects.filter(organization=org, user=user).exclude(pk=employee.pk).first()
    if other is not None:
        raise InviteError("That email address already belongs to another employee's account.")
    Membership.objects.get_or_create(user=user, organization=org, defaults={"role": Role.EMPLOYEE})
    if employee.user_id != user.pk:
        employee.user = user
        employee.save(update_fields=["user"])

    key = f"{user_pk_to_url_str(user)}-{default_token_generator.make_token(user)}"
    link = get_reset_password_from_key_url(request, key)
    context = {"link": link, "organization": org.name, "site_name": settings.STACKS_SITE_NAME}
    subject = f"{settings.STACKS_SITE_NAME}: set up your account"
    body = render_to_string("people/email/invite.txt", context)
    address = user.email
    transaction.on_commit(lambda: send_mail(subject, body, None, [address]))
    return user
