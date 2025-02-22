import html
import logging
import re
import sys
import time

import jsonschema
from praw import Reddit
from praw.models.reddit.comment import Comment
from praw.models.reddit.submission import Submission
from prawcore.exceptions import NotFound
import yaml


REGEX_RULE = re.compile(r"[@!]say (\w*) *(.*)", re.IGNORECASE)
REGEX_TEMP_BAN = re.compile(
    r'[@!]notinuse (\d*) "([^"]*)" "([^"]*)"', re.IGNORECASE
)
REGEX_PERM_BAN = re.compile(r'[@!]notinuse "([^"]*)" "([^"]*)"', re.IGNORECASE)
REGEX_REFRESH = re.compile(r"[@!]refreshsay (.*)", re.IGNORECASE)
REGEX_SPAM = re.compile(r"[@!]unused$", re.IGNORECASE)

SCHEMA_VALIDATOR = jsonschema.Draft7Validator(
    yaml.safe_load(
        r"""
        type: object
        required:
            - Header
            - Footer
            - Generic
        properties:
            Header:
                type: string
            Footer:
                type: string
        additionalProperties:
            type: object
            properties:
                Flair:
                    type: string
                Message:
                    type: string
        propertyNames:
            pattern: "^\\w+$"
        """
    )
)


class Bot:
    def __init__(self, r):
        self.r = r
        logging.debug("Success.")
        self.logging_enabled = True
        self.subreddits = {}
        for subreddit in SUBREDDITS:
            logging.info("Checking subreddit: %s…", subreddit)
            mods, reasons = self.load_sub_config(subreddit)
            self.subreddits[subreddit] = {
                "mods": mods,
                "reasons": reasons,
            }

    def load_sub_config(self, subreddit):
        logging.debug("Loading mods…")
        mods = [mod.name for mod in self.r.subreddit(subreddit).moderator()]
        logging.info("Mods loaded: %s.", mods)
        logging.debug("Loading reasons…")
        try:
            reasons = yaml.safe_load(
                html.unescape(
                    self.r.subreddit(subreddit).wiki["saybot"].content_md
                )
            )
            SCHEMA_VALIDATOR.validate(reasons)
            logging.info("Reasons loaded.")
        except (jsonschema.exceptions.ValidationError, NotFound):
            reasons = None
            logging.warning(
                "r/%s/wiki/saybot not found or invalid, ignoring", subreddit
            )
        return mods, reasons

    def refresh_sub(self, subreddit):
        logging.info("Refreshing subreddit: %s…", subreddit)
        mods, reasons = self.load_sub_config(subreddit)
        sub = self.subreddits[subreddit]
        sub["mods"] = mods
        if reasons is not None:
            sub["reasons"] = reasons

    def check_comments(self, subreddit):
        logging.debug("Checking subreddit: %s…", subreddit)
        sub = self.subreddits[subreddit]
        for comment in self.r.subreddit(subreddit).comments(limit=100):
            if (
                comment.banned_by
                or not comment.author
                or comment.author.name not in sub["mods"]
            ):
                continue

            report = {
                "source": comment,
                "reason": comment.body,
                "author": comment.author.name,
            }
            self.handle_report(subreddit, report, comment.parent())

    def check_reports(self, subreddit):
        logging.debug("Checking subreddit reports: %s…", subreddit)
        for reported_submission in self.r.subreddit(subreddit).mod.reports():
            if not reported_submission.mod_reports:
                continue

            report = {
                "source": None,
                "reason": reported_submission.mod_reports[0][0],
                "author": reported_submission.mod_reports[0][1],
            }
            self.handle_report(subreddit, report, reported_submission)

    def handle_report(self, subreddit, report, target):
        sub = self.subreddits[subreddit]
        # Check for @rule command.
        match = REGEX_RULE.search(report["reason"])
        if match:
            rule = match.group(1)
            note = match.group(2)
            logging.info("Comment %s matched.", rule)
            if rule not in sub["reasons"]:
                rule = "Generic"
            msg = sub["reasons"][rule]["Message"]
            if note:
                msg = f"{msg}\n\n{note}"

            if report["source"] is not None:
                report["source"].mod.approve()
            target.mod.approve()

            author = target.author.name if target.author is not None else "OP"
            header = sub["reasons"]["Header"].format(author=author)
            footer = sub["reasons"]["Footer"].format(author=author)
            msg = f"{msg}"
            target.reply(msg).mod.distinguish(sticky=True)

            if isinstance(target, Submission):
                logging.info("Posted comment.")
                target.mod.flair(sub["reasons"][rule]["Flair"])
            elif isinstance(target, Comment):
                logging.info("Removed comment.")

            permalink = target.permalink
            self.log(subreddit, f"{report['author']} removed {permalink}")
        # Check for @spam command.
        if REGEX_SPAM.search(report["reason"]):
            if report["source"] is not None:
                report["source"].mod.remove()
            target.mod.remove(spam=True)
            if isinstance(target, Submission):
                logging.info("Removed submission (spam).")
                permalink = target.permalink
            elif isinstance(target, Comment):
                logging.info("Removed comment (spam).")
                permalink = target.permalink(fast=True)
            self.log(
                subreddit, f"{report['author']} removed {permalink} (spam)"
            )
        # Check for @ban command.
        temp_match = REGEX_TEMP_BAN.search(report["reason"])
        perma_match = REGEX_PERM_BAN.search(report["reason"])
        if temp_match or perma_match:
            if target.author is None:
                logging.info("Skipping ban for [deleted] user")
            elif temp_match:
                duration = temp_match.group(1)
                reason = temp_match.group(2)
                msg = temp_match.group(3)
                logging.info(
                    "Ban (%s: %s -- %s) matched.", duration, reason, msg
                )
                self.r.subreddit(subreddit).banned.add(
                    target.author.name,
                    duration=duration,
                    note=reason,
                    ban_message=msg,
                )
            elif perma_match:
                reason = perma_match.group(1)
                msg = perma_match.group(2)
                logging.info("Ban (Permanent: %s -- %s) matched.", reason, msg)
                self.r.subreddit(subreddit).banned.add(
                    target.author.name, note=reason, ban_message=msg
                )
            if report["source"] is not None:
                report["source"].mod.remove()
            target.mod.remove()
            if target.author is not None:
                logging.info("User banned.")
                self.log(
                    subreddit,
                    f"{report['author']} banned u/{target.author.name}",
                )

    def log(self, subreddit, msg):
        if not self.logging_enabled:
            return
        logs_page = self.r.subreddit(subreddit).wiki["saybot_logs"]
        try:
            logs_content = logs_page.content_md
        except TypeError:
            logs_content = ""
        except NotFound:
            logging.warning(
                "r/%s/wiki/saybot_logs not found, disabling logging",
                subreddit,
            )
            self.logging_enabled = False
            return
        logs_page.edit(f"{logs_content}{msg}  \n")

    def check_mail(self):
        logging.debug("Checking mail…")
        for mail in self.r.inbox.unread():
            mail.mark_read()
            logging.info('New mail: "%s".', mail.body)
            match = REGEX_REFRESH.search(mail.body)
            if not match:
                continue
            subreddit = match.group(1)
            if subreddit in self.subreddits:
                sub = self.subreddits[subreddit]
                if mail.author in sub["mods"]:
                    self.refresh_sub(subreddit)
                    mail.reply(f"Refreshed mods and reasons for {subreddit}!")
                else:
                    mail.reply(f"Unauthorized: not an r/{subreddit} mod")
            else:
                mail.reply(f"Unrecognized sub: {subreddit}.")

    def run(self):
        while True:
            logging.debug("Running cycle…")
            for subreddit in SUBREDDITS:
                if self.subreddits[subreddit]["reasons"] is None:
                    continue
                try:
                    self.check_comments(subreddit)
                    self.check_reports(subreddit)
                except Exception as exception:
                    logging.exception(exception)
            try:
                self.check_mail()
            except Exception as exception:
                logging.exception(exception)
            logging.debug("Sleeping…")
            time.sleep(32)  # PRAW caches responses for 30s.


if __name__ == "__main__":
    with open("config.yaml") as config_file:
        CONFIG = yaml.safe_load(config_file)
        CLIENT_ID = CONFIG["Client ID"]
        CLIENT_SECRET = CONFIG["Client Secret"]
        USERNAME = CONFIG["Username"]
        PASSWORD = CONFIG["Password"]
        SUBREDDITS = CONFIG["Subreddits"]
        USER_AGENT = CONFIG["User Agent"]

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    logging.info("Logging in…")
    MODBOT = Bot(
        Reddit(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            user_agent=USER_AGENT,
            username=USERNAME,
            password=PASSWORD,
        )
    )
    MODBOT.run()
