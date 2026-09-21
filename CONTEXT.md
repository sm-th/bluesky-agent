# Bluesky Research

A public dialogue in which one authorized human starts and continues research through Bluesky, while an agent publishes concise replies and maintains the supporting knowledge base.

## Language

**Operator**:
The only human identity authorized to start or continue Research, identified by its Bluesky DID.
_Avoid_: User, requester, account

**Agent**:
The public Bluesky identity that researches Research Turns, replies to them, and maintains the Research Wiki.
_Avoid_: Bot, assistant

**Research Root**:
An original Bluesky post authored by the Operator after Activation. Every Research Root starts Research; the Operator's replies under other authors' roots do not.
_Avoid_: Question, request, prompt

**Research Turn**:
A Research Root or a later Operator-authored post within its reply tree. Every Research Turn receives its own Research, Brief Reply, and Research Page.
_Avoid_: Message, query

**Research Branch**:
One ancestry path through a Research Root's reply tree. Sibling branches never contribute context to each other.
_Avoid_: Thread, conversation

**Research Session**:
The Agent's durable working context for one Research Branch. A child branch inherits its parent's context at the fork and then evolves independently.
_Avoid_: Transcript, chat history

**Working Mark**:
The Agent's permanent like on a Research Turn, showing that the turn was accepted for autonomous processing.
_Avoid_: Approval, completion marker

**Activation**:
The moment from which new Research Roots become eligible for Research. Earlier posts are outside the backlog.
_Avoid_: Migration, initial import

**Publication Language**:
English, used for every Agent reply, Research Page, Page Screenshot, and maintained wiki page regardless of source language.
_Avoid_: Automatic language matching, translation mode

**Research**:
The Agent's source-grounded investigation of a Research Turn, whether that turn asks a question, advances an argument, or requests discovery on the internet.
_Avoid_: Report, deep dive

**Research Mode**:
The presentation shape selected for a Research Turn: Source Brief, Question Answer, or Note Exploration. A clear question or source request determines its mode directly; an otherwise ambiguous turn may be classified without changing the Agent's permissions.
_Avoid_: Agent mode, capability mode

**Source Brief**:
A source-centered treatment that explains what supplied or linked evidence supports, including material limits or disagreement.
_Avoid_: Link summary, generic explainer

**Question Answer**:
A direct resolution of the Research Turn's substantive question, led by the answer and bounded by the evidence.
_Avoid_: FAQ, response mode

**Note Exploration**:
An evidence-grounded examination of an observation, claim, topic, or inherited line of inquiry.
_Avoid_: Freeform mode, brainstorm

**Brief Reply**:
The Agent's direct Bluesky response to a Research Turn, limited to 300 graphemes and focused on the substance rather than work performed.
_Avoid_: Summary, status update

**Expanded Answer**:
The mode-specific standalone treatment of a Research Turn that adds detail beyond the Brief Reply, fits on one readable Page Screenshot, and remains understandable without the Brief Reply.
_Avoid_: Transcript, long article

**Research Page**:
The compact public page for one Research Turn, presenting its Expanded Answer and evidence while participating in the Research Wiki.
_Avoid_: Blog post, report

**Page Screenshot**:
An image of the entire compact Research Page attached to the Brief Reply so the Expanded Answer can be read directly in Bluesky.
_Avoid_: Generated answer card

**Research Wiki**:
The shared LLM-maintained knowledge base compiled incrementally from preserved sources and Research. Every Research Branch may read its latest published state, but it does not inherit generated content from the Retired Wiki.
_Avoid_: Old wiki, archive

**Retired Wiki**:
The previous Smith Wiki, removed from publication after the Research Wiki replaces it but retained in version history.
_Avoid_: Migrated wiki
