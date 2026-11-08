# Artifact Delivery Rules

## Required delivery behavior

- When the user asks for a file, create and verify the actual artifact first.
- Deliver the artifact as a native attachment using `MEDIA:/absolute/path/to/file`.
- Do not use a `sandbox:` link as the only delivery method.
- Do not claim that a file was copied to the user's Mac unless there is a verified receipt from the Mac.
- If the artifact was created on another machine, attach it directly so the user can download it on the Mac.
- If a local Downloads folder is unavailable or read-only, state that plainly and still attach the verified source file.
- Before delivery, verify the file exists, has non-zero size, and is valid for its requested format.
