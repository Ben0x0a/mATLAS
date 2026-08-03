# Glossary

mATLAS reuses ordinary words with deliberate, narrow meanings. This is the one place each term
is defined; the other documents use them without re-defining. Terms are alphabetical;
*italicised* words in a definition are themselves defined here.

---

**Activity**
The event a source record describes — a call, a message, a visit, a sighting. An activity groups
the *assertions* that belong to one event: a call is one activity with two placed legs, each its
own assertion. An activity is linked to exactly one raw record at ingest.

**Append**
Running against an existing *case* database to add a new *run* to it, rather than creating a new
case. The new run's evidence is added under the existing *case* identity. See also *create*.

**Assertion**
The atomic unit of evidence: one placed claim drawn from one *raw record* by one *preset* — a
single *entity*, at most one *position*, at most one time. An assertion is one output line only
when it is a *placement*. Its identity is content-addressed (see *content hash*, *claim digest*).

**Case**
One investigation. A case lives in exactly one `*.matlas.sqlite` database and roots everything
below it: the *runs* recorded against it, their *sources*, and every derived value.

**Claim digest**
A content hash computed over an assertion's Layer-1 claim projection alone (independent of which
*source* or *run* produced it). Two sources making the same claim share a claim digest; it never
merges them — sameness is a query, not a stored merge (see *convergence*).

**Concerns**
The association an *entity* carries to the case subject the data is *about* — "whose" the record
is. The operator supplies it as the run's subject (a preset may map it per-row); it is carried on
the entity row, distinct from the entity's own identifier.

**Concomitance**
The analysis of who or what was *present* together in space and time. A *presence* question:
merely *referenced* places and *negated* links do not count; an unknown link does.

**Content hash**
The content-addressed identity of an assertion, computed from its evidence fingerprint, preset,
source line, output ordinal, and Layer-1 projection. No *run* or *case* identifier feeds it, which
is why *append* is deterministic and why a file read from a folder or a zip yields the same
identity ("folder == zip" parity).

**Convergence**
Two *entities* being "the same" as the result of grouping by *(type, canonical identifier)* at
read time. mATLAS never merges, deduplicates, or deletes entity rows — convergence is always a
query over the *occurrence-keyed* rows.

**Create**
Running against a *case*-database path that does not yet exist: the run mints the case identity
and writes the first *run*. Contrast *append*.

**Component**
One verbatim part of an *expression*, stored as its own row: one component per source field. A
geohash expression has one component; a latitude+longitude pair has two; a date column plus a
time column has one per bound. Components hold the source values exactly as given (*verbatim*).

**Derived** — see *Layer 2*.

**Entity**
The subject an *assertion* is about: a device, account, or person identifier, stored as one lean
row of *(type, identifier)* plus the *concerns* association. The identifier is *canonicalised* at
*integration* (or the normaliser *refuses*).

**Exhibit**
The physical or logical evidence item an acquisition delivered (a disk image, an extraction, a
file), referenced from a *source* so a claim can be traced back to what was seized.

**Expression**
A header row for a spatial or temporal value, made up of one or more *components*. The expression
records the *referential* and role; the components hold the verbatim source values. Splitting the
value into header + components is what lets one value come from one *or several* source fields.

**Integration**
The Layer-2 step that turns an *expression*'s verbatim *components* into a canonical value (a WGS84
coordinate, a UTC instant, a canonical identifier) — always stamped with the *method* that did it.

**Layer 0 / Layer 1 / Layer 2 / Layer 3**
The four tiers of the run database. **L0** = acquisition (what was opened: *runs*, *sources*, *raw
records*). **L1** = the *verbatim* assertion (the source's claim, un-transformed). **L2** = *derived*,
method-stamped values (canonical space/time, normalised identifiers). **L3** = *analysis* (judgements
about assertions, e.g. duplicate ranking, concomitance).

**Locates**
The derived flag on a projected row saying what the assertion's *position* locates — the *entity*
itself, or another entity it points at. Present on every flat-CSV row so a self-location and a
located-other never render alike.

**Method**
The named, versioned rule that produced a *derived* value, written `rule/version` (optionally
`/reference`). A pass-through is still a method. Every Layer-2 value names one, so two look-alike
values (a GPS fix vs a geocoder centroid) are always distinguishable.

**Mention**
A party *referenced* in a record without its own *placement* — a facet, a counterpart, a context
reference. A mention attaches to an *assertion*; it never earns its own output line.

**Negation**
A link asserting something did *not* co-occur ("never during this interval"). Excluded from
*concomitance* (which counts *presence*).

**No silent loss**
The governing rule: nothing an operator provides is dropped without a record. An unreadable file,
a skipped archive, a duplicate re-append, an unmapped field — each is reported or recorded, never
quietly discarded.

**Occurrence key vs content key**
An *occurrence-keyed* row exists once per occurrence (each observation, each person mention is its
own row); a *content-keyed* row would be keyed by its value and so silently merge two different
things that share a value (two people named "John Smith"). mATLAS is occurrence-keyed by design;
sameness is decided by *convergence*, not by the key.

**Placement**
A located event — an *assertion* that has a position and/or a time of its own. A placement earns
one output line; a party with no placement of its own is a *mention*, not a line.

**Position**
The spatial part of an assertion (a coordinate, a cell, a place), expressed via a spatial
*expression* and, once *integrated*, a canonical WGS84 coordinate and spatial index.

**Preset**
A declarative YAML rule set describing which files it applies to and how to map their fields into
*assertions*. Presets are matched to *sources* and applied to their *raw records*.

**Presence** — see *concomitance*. What an assertion's *placement* establishes: the subject *was*
there, as opposed to merely having *referenced* the place.

**Raw record**
One verbatim row within a *source* (a CSV line, a table row, a sheet row), stored exactly as read.
The unit a *preset* is applied to; an *assertion* derives from one raw record.

**Referential**
The frame a value is expressed in: for space, the coordinate reference system / encoding (e.g.
`epsg:4326`, a geohash); for time, the temporal frame. Recorded on the *expression* so the same
number in two frames is never treated as equal.

**Refuse**
The normaliser's verdict when a raw identifier cannot be *canonicalised* safely: the canonical
identifier is left empty so nothing can *converge* on it, and the verbatim value survives. Chosen
over guessing, because a wrong canonical form silently merges two different subjects.

**Run**
One invocation of the pipeline against a *case* — one `process()` call. A run *creates* the case or
*appends* to it, and records the complete operator settings that produced it.

**Source**
A unit that was *opened* and read: a file, or a sub-unit of one (a SQLite table, an Excel sheet, an
XML model type). Identified by its path (not its bytes). Holds *raw records*.

**Source tier**
The nature of a source declared by the matched *preset* — e.g. `primary` vs `secondary` evidence.
A source-grain attribute, outside the evidence digests.

**Verbatim** — see *Layer 1*. A value stored exactly as the source gave it, with no unit, format,
or type conversion (text *extraction* is allowed, since it selects verbatim characters).
