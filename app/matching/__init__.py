"""Candidate-to-job-order matching, computed locally — no model, no API, no cost.

The pieces, in the order a ranking uses them:

    spec        a job order, normalised into requirements that are either
                mandatory or preferred, and never invented
    pool        who is matched: everyone not yet submitted, in one query
    evidence    one candidate's existing profile, read for the facts a match
                turns on — experience, documents, quotable lines
    vocabulary  how CVs and orders say the same thing differently: stemming,
                synonyms (gtaw = tig = argon arc), and the trade table
    matcher     the ranking: trade and title match, skills with synonyms and
                typo tolerance, BM25 relevance, experience, qualifications,
                destination, documents — for the whole pool, in memory
    scoring     weights, the mandatory gate, and the categories
    store       recruiter decisions, kept across re-ranking
    evaluation  how well the ranking agrees with those decisions
    api         the routes Job Orders calls

`matching score` is what this package computes: how well the evidence on file
answers the requirements of one job order. It is not a probability of hire, and
nothing here should ever present it as one.
"""
