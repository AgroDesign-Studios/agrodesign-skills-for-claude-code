# The values file — your private values, kept outside git

The generic patterns of the scan (private IP ranges, `.local`/`.internal` host names, e-mail addresses, home-directory paths, key and token shapes) cannot know *your* names: the build server's host name, your OS account, the office network, a customer's name, the folder where your keys live. You list them once, in a file no repository holds, and every scan matches each of them.

## Where

`$XDG_CONFIG_HOME/agrodesign-github-upload/values.json`, or `~/.config/agrodesign-github-upload/values.json` when `XDG_CONFIG_HOME` is not set. This is the only file a seal accepts.

- The file must lie outside every git work tree (no `.git` in any folder above it): the scan refuses one inside, because it could be committed and published. The check looks at the path as written and with every link resolved (a config folder linked into a dotfiles repository is refused), and asks git itself. If your home folder is itself a git repository (dotfiles), point `XDG_CONFIG_HOME` at a folder outside it for the runs of this skill. A *bare* dotfiles repository whose work tree is your home only through its own config cannot be seen from the file: never add the values file to it, and list it in that repository's exclude file.
- It must be a regular file (not a symbolic link) of at most 1 MiB, UTF-8 JSON.
- Keep it readable by you alone: `chmod 600`.

## Format

```json
{
  "format": 1,
  "_comment": "anything starting with _ is a comment",
  "values": {
    "host.build": "build-07.corp.example",
    "host.nas": "nas-shared.corp.example",
    "net.office": "10.20.30.",
    "account.ci": "ci-runner",
    "person.owner": "Jordan Example",
    "mail.owner": "jordan@example.com",
    "path.keys": "/opt/team-keys/",
    "project.codename": "bluefinch",
    "word.short": {"value": "ab", "scan": false}
  }
}
```

(Every value above is made up; write your own.)

- **Keys** are labels you choose: lowercase letters, digits, `.`, `_` and `-`, at most 64 characters. The scan names a hit by its key (`value host.build`), never by the value, so a key should not itself be secret.
- **Values** are matched case-insensitively as substrings, anywhere in the set: file contents, file names, archive members, decoded base64 payloads, commit messages and identities. A value is 3 to 1024 characters with no surrounding blanks or control characters.
- `{"value": "...", "scan": false}` keeps a value in the file but out of the scan — for a value too short or too common to match literally (it would hit everywhere). A scanned value needs at least 3 characters.
- A secret itself (a password, a token) does not belong here: gitleaks and the secret patterns find those by shape, and this file should hold nothing that grants access. List where secrets are kept (a path, a vault host) instead.

## What the scan and the seal do with it

- `scan` reads the file once, records its path, its sha256 and how many values it matched (`VALUES checked (12 value(s) from …)`), and reports each match as a `private_value` hit to triage.
- `seal`, `verify` and `push` read the file again and refuse when:
  - the scan did not read it (`missing`), or it held no scanned value (`empty`);
  - the scan read another file (`--values-file`, a trial only);
  - its bytes changed since the scan (a value added since would go out unchecked) — scan again.
- No owner's words waive this: without your values the check cannot know your names.

## Keeping it current

Add a value whenever a new name appears: a new machine, a new account, a new customer. A run after the change must scan again; the seal tells you so.
