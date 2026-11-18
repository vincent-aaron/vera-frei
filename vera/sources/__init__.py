"""Job sources. Only free, public APIs that allow this use; each listing keeps a
link back to the original job page and credits its source, as the APIs ask.

LinkedIn, JobStreet, Indeed, Kalibrr and Bossjob have no free public API, so
jobs from those sites are added with /add-job (paste the link and description).
"""

from vera.sources.base import Source, clean_html, fingerprint  # noqa: F401
from vera.sources.extra import CompanyForms, HNHiring
from vera.sources.feeds import ArbeitNow, Himalayas, Jobicy, Jobspresso, LaraJobs, RealWorkFromAnywhere, Remotive, RemoteOK, TheMuse, WeWorkRemotely, WorkingNomads

SOURCES = {s.name: s for s in (Remotive(), ArbeitNow(), Jobicy(), Himalayas(), TheMuse(),
                                RemoteOK(), WeWorkRemotely(), WorkingNomads(), CompanyForms(), HNHiring(),
                                Jobspresso(), RealWorkFromAnywhere(), LaraJobs())}
LABELS = {"remotive": "Remotive", "arbeitnow": "Arbeitnow", "jobicy": "Jobicy", "himalayas": "Himalayas",
          "themuse": "The Muse", "remoteok": "Remote OK", "weworkremotely": "We Work Remotely",
          "workingnomads": "Working Nomads", "companyforms": "Company job lists", "hnhiring": "Hacker News hiring", "jobspresso": "Jobspresso", "realworkfromanywhere": "Real Work From Anywhere",
          "larajobs": "LaraJobs", "manual": "Added by you"}
