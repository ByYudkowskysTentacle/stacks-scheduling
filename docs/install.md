# Installing, backing up and upgrading Stacks

This guide is for a library or campus IT team running Stacks on one server with Docker. It covers a fresh install, backups and restores, upgrades, and the privacy questions to settle before real student data goes in.

## Before you start: privacy sign-off

Stacks holds student employee records: availability, scheduled hours and, if you enter them, wage rates. At US institutions these may be protected education records under FERPA (SPEC §11).

- If the server is off campus (a VPS), check with your registrar or counsel whether a data-processing agreement is needed **before** importing real data (decisions D8 and D50, open item 4).
- Wage fields are optional. Leave them blank until hosting is signed off, if you prefer (D48).
- Stacks never sends wages, hour totals or co-workers' details by email, and it never logs personal data.

## What you need

| Item | Recommended |
|---|---|
| Server | Ubuntu 24.04 LTS (or any Linux with Docker), 4 vCPU, 8 GB RAM, 40 GB disk. The solver uses the CPUs; 2 vCPU works but rebalances are slightly weaker (ADR 0001). |
| Software | Docker Engine with the Compose plugin (`docker compose version` should work), `git`, and `age` for encrypted backups. |
| Network | A DNS name (for example `stacks.example.edu`) pointing at the server, and ports 80 and 443 open to the internet so Caddy can get a certificate. Nothing else needs to be open apart from SSH. |
| Email | An SMTP account Stacks can send from (invitations and notification emails). |

Basic hardening: SSH keys only, a firewall allowing 22, 80 and 443 (`ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`), and unattended security upgrades.

## Install

```bash
sudo mkdir -p /opt/stacks && sudo chown "$USER" /opt/stacks
git clone https://github.com/ByYudkowskysTentacle/stacks-scheduling.git /opt/stacks
cd /opt/stacks
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

- `SITE_ADDRESS`, `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`: your DNS name.
- `DJANGO_SECRET_KEY`: generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- `POSTGRES_PASSWORD`: a long random password.
- `SMTP_*` and `DEFAULT_FROM_EMAIL`: your mail settings.
- `STACKS_TIME_ZONE`: the library's time zone, for example `America/Chicago`.
- `WORKER_CPUS`: the CPUs the solver may use (4 on the pilot server).

Then start it and create the first admin:

```bash
docker compose up -d --build
docker compose run --rm web python manage.py bootstrap \
  --organization "Your Library" --admin-email you@example.edu
```

`bootstrap` asks for the admin's password (at least 12 characters). Sign in at `https://your-dns-name/` and follow the setup checklist on the home page: location and opening hours, closures, terms, pay roles, then people (by hand or from the roster CSV, `docs/formats/README.md`).

What's running (`docker compose ps`):

| Service | Role |
|---|---|
| `caddy` | HTTPS with automatic certificates. The only service with open ports. |
| `web` | The Django app. |
| `worker` | Runs schedule generation and rebalancing in the background. |
| `db` | PostgreSQL. Not reachable from outside the server. |
| `migrate` | Runs database migrations at start-up, then exits. |

## Backups

Back up the database every day, encrypted, to somewhere off the server. `deploy/backup.sh` writes an [age](https://age-encryption.org/)-encrypted dump; the decryption key never lives on the server.

One-time setup, on a machine that is **not** the server (for example the IT team's password manager or an offline laptop):

```bash
age-keygen -o stacks-backup-key.txt     # keep this file safe: it's the only way to restore
age-keygen -y stacks-backup-key.txt     # prints the public key, age1...
```

On the server, put the public key in `deploy/backup-recipient.txt` and schedule the backup:

```bash
echo "age1...your public key..." > /opt/stacks/deploy/backup-recipient.txt
sudo apt install age
crontab -e
# 15 2 * * * cd /opt/stacks && deploy/backup.sh /var/backups/stacks >> /var/log/stacks-backup.log 2>&1
```

The script keeps 30 days locally (`KEEP_DAYS` to change it). **Copy `/var/backups/stacks` off the server** with your usual tool (rclone, restic, rsync to campus storage). A backup that only exists on the server it backs up isn't a backup.

Also keep a copy of `.env` somewhere safe: without `DJANGO_SECRET_KEY` and the database password a restore still works, but signed-in sessions and password-reset links reset.

## Restore

Test this once after installing, and after major upgrades, on a spare server if you can. It was tested on the compose stack in this repository.

```bash
cd /opt/stacks
docker compose down                     # stop everything
docker compose up -d db                 # start only the database
age --decrypt -i stacks-backup-key.txt stacks-YYYYmmdd-HHMMSS.dump.age \
  | docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose up -d                    # start the rest; migrations run automatically
```

To restore onto a brand-new server, install as above (same `.env`), run `docker compose up -d db`, then the restore commands.

## Upgrades

```bash
cd /opt/stacks
deploy/backup.sh /var/backups/stacks    # always back up first
git pull
docker compose up -d --build
docker compose logs migrate             # should end with the migrations applied, or "No migrations to apply"
```

To roll back, check out the previous version (`git checkout <tag>`), rebuild, and restore the backup taken before the upgrade. Database migrations only run forwards.

## Day to day

- **Logs:** `docker compose logs -f web worker`. Logs hold request paths and status codes, never personal data.
- **Solver speed:** generate runs for up to `STACKS_GENERATE_SECONDS` (default 300), and a rebalance for `STACKS_REBALANCE_SECONDS` (default 30). Results that hit the limit are labelled "not proven optimal".
- **Accounts:** supervisors invite employees from their record in People. Admins grant supervisor and admin roles under Settings, Roles.
- **Leaving Homebase:** until you stop using Homebase, a published schedule can be downloaded as CSV (Schedule, Versions, a version, Download CSV) for re-entry there if you ever need to fall back (D47). The file holds names and emails, so delete it when you're done.
- **Retention:** nothing is purged until an admin sets periods under Settings, Retention (SPEC §11). The worker then purges expired data every night at 03:17 server time (UTC in the default containers). To see what is due without removing anything, run `docker compose exec web python manage.py purge_expired --dry-run`. Purged data is gone from the database, but stays in backups until those age out (30 days by default), so match backup retention to your policy.
- **Records requests:** a supervisor can download everything held about one person from their record in People. An admin can erase an archived person's personal data there too. Erasure clears the live record but keeps the audit log and stored messages as they are (D100, D101), and doesn't reach existing backups.
- **Availability reminders:** set a deadline on the term (Settings, Terms). Reminder emails go out only if you tick "Email availability reminders" there (on the reminder days you list, for example 7, 3, 1 days before, and on the deadline day), or press "Send reminders now" on the availability grid's reminders page. Each reminder also appears in the person's Notifications list, and each person can turn reminder emails off under Notifications, Email settings. The worker sends scheduled ones at 14:05 server time, and links in them use `STACKS_SITE_URL` (by default `https://` plus `SITE_ADDRESS`).
