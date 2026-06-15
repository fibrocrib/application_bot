"""Schema for cv_content.py. Copy to cv_content.py, fill in your details, and
the local build_cv.py + the per-application cv_tailor will use it.

In CI, the real cv_content.py is materialised from the CV_CONTENT_B64
GitHub Actions secret — see .github/workflows/daily-apply.yml.

To generate the secret value:
  base64 -i cv_content.py | pbcopy        # macOS
  base64 -w0 cv_content.py | xclip -selection clipboard   # Linux
Then paste into a new repo secret named CV_CONTENT_B64."""

NAME = "Your Name"
CONTACT = "City  ·  +44 7XXX XXXXXX  ·  you@example.com  ·  github.com/yourhandle"

DEFAULT_SUMMARY = (
    "One-paragraph personal statement that sits at the top of the CV. "
    "Per-application, src/cv_tailor.py rewrites this for the specific job."
)

WORK = [
    {
        "company": "Most Recent Company",
        "role": "Job Title",
        "dates": "MONTH YEAR – PRESENT",
        "bullets": [
            "First impact bullet.",
            "Second impact bullet.",
        ],
    },
]

PROJECTS = [
    {
        "title": "Project Name  (Tech stack)",
        "bullets": [
            "What you built and what it does.",
            "Notable technical detail or outcome.",
        ],
    },
]

SKILLS = [
    ("Languages", "Python, ..."),
    ("Other", "..."),
]

EDUCATION = [
    {
        "school": "University",
        "degree": "Degree — Subject",
        "dates": "MONTH YEAR – MONTH YEAR",
        "detail": "Optional one-paragraph description.",
    },
]
