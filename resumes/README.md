Put your resume files here.

`config/preferences.yaml` -> `resume.default` and `resume.variants` point at
this folder. The variant whose `match_keywords` first hit the job text wins;
otherwise the default is used.

    resumes/base.docx        fallback
    resumes/backend.docx     backend / .NET / API roles
    resumes/fullstack.docx   React / Node / MERN roles

This folder is gitignored.
