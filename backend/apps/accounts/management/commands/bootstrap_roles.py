import os

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from apps.accounts.permissions import ROLES


class Command(BaseCommand):
    help = "Create viewer/operator/admin groups and optionally an admin user from ADMIN_USERNAME/ADMIN_PASSWORD."

    def handle(self, *args, **options):
        for role in ROLES:
            Group.objects.get_or_create(name=role)
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        if username and password:
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password(password)
                user.is_staff = True
                user.save()
            user.groups.add(Group.objects.get(name="admin"))
            self.stdout.write(f"admin user '{username}' {'created' if created else 'exists'}")
        self.stdout.write(self.style.SUCCESS("roles ready: " + ", ".join(ROLES)))
