"""Example build graph: a small three-stage text pipeline.

    data/docN.txt --tokenize--> out/docN.tokens --merge--> out/all.tokens --report--> out/report.txt

Build it with::

    python -m buildcache build -p examples/demo_project

Then edit one ``data/docN.txt`` and build again: only that document's tokens,
the merge and the report are rebuilt - the other two tokenizers stay cached.
"""

from buildcache import Target

DOCS = ("doc1", "doc2", "doc3")

TARGETS = []

# Stage 1: one tokenizer per document. Each depends only on its own source file.
for doc in DOCS:
    TARGETS.append(
        Target(
            name=f"tokenize-{doc}",
            command=["python", "tools/tokenize.py", f"data/{doc}.txt", f"out/{doc}.tokens"],
            inputs=[f"data/{doc}.txt", "tools/tokenize.py"],
            outputs=[f"out/{doc}.tokens"],
            description=f"tokenize {doc}.txt",
        )
    )

# Stage 2: merge every token stream into one file.
TARGETS.append(
    Target(
        name="merge",
        command=[
            "python", "tools/merge.py",
            "out/doc1.tokens", "out/doc2.tokens", "out/doc3.tokens", "out/all.tokens",
        ],
        inputs=["tools/merge.py"],
        outputs=["out/all.tokens"],
        deps=[f"tokenize-{doc}" for doc in DOCS],
        description="merge the token streams",
    )
)

# Stage 3: produce the final report from the merged stream.
TARGETS.append(
    Target(
        name="report",
        command=["python", "tools/count.py", "out/all.tokens", "out/report.txt"],
        inputs=["tools/count.py"],
        outputs=["out/report.txt"],
        deps=["merge"],
        description="count tokens and write the report",
    )
)

# A phony convenience target that just groups everything.
TARGETS.append(
    Target(
        name="all",
        deps=["report"],
        phony=True,
        description="build everything",
    )
)
