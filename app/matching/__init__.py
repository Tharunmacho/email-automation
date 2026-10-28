"""Candidate-to-job-order matching, scored by Jev (TypeSafe System One).

The pieces, in the order a run uses them:

    spec        a job order, normalised into requirements that are either
                mandatory or preferred, and never invented
    prefilter   the Mongo query and the rule-based ranking that decide which
                candidates are worth paying a model to read
    evidence    one candidate's existing OCR-extracted profile, reduced to the
                fields that bear on the decision and nothing else
    questions   the versioned decision schema: the choice, score and noul
                questions Jev answers about that pair
    jev         the HTTP door to the vendor, with the timeout, the retries and
                the concurrency cap on it
    scoring     Jev's answers and the mandatory gate, combined into a 0-100
                matching score and a category
    store       what was decided, kept so it is not paid for twice
    runner      the background job that drives all of the above
    api         the routes Job Orders calls

`matching score` is what this package computes: how well the evidence on file
answers the requirements of one job order. It is not a probability of hire, and
nothing here should ever present it as one.
"""
