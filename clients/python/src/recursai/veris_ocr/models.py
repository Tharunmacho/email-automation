class MRZResult:
    def __init__(self, passport_number: str, expiry_date: str, **kwargs):
        self.passport_number = passport_number
        self.expiry_date = expiry_date
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def from_dict(cls, data: dict) -> "MRZResult":
        if not data:
            return cls(passport_number="", expiry_date="")
        return cls(
            passport_number=data.get("passport_number", ""),
            expiry_date=data.get("expiry_date", ""),
            **{k: v for k, v in data.items() if k not in ("passport_number", "expiry_date")}
        )

    def __repr__(self) -> str:
        return f"<MRZResult passport_number={self.passport_number!r} expiry_date={self.expiry_date!r}>"


class PassportResult:
    def __init__(self, mrz: MRZResult, **kwargs):
        self.mrz = mrz
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def from_dict(cls, data: dict) -> "PassportResult":
        if not data:
            return cls(mrz=MRZResult.from_dict({}))
        mrz_data = data.get("mrz", {})
        mrz = MRZResult.from_dict(mrz_data)
        return cls(
            mrz=mrz,
            **{k: v for k, v in data.items() if k != "mrz"}
        )

    def __repr__(self) -> str:
        return f"<PassportResult mrz={self.mrz!r}>"


class DocumentPage:
    def __init__(self, text: str, **kwargs):
        self.text = text
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentPage":
        if not data:
            return cls(text="")
        return cls(
            text=data.get("text", ""),
            **{k: v for k, v in data.items() if k != "text"}
        )

    def __repr__(self) -> str:
        snippet = self.text[:30] + "..." if len(self.text) > 30 else self.text
        return f"<DocumentPage text={snippet!r}>"


class DocumentResult:
    def __init__(self, page_count: int, pages: list[DocumentPage], **kwargs):
        self.page_count = page_count
        self.pages = pages
        for k, v in kwargs.items():
            setattr(self, k, v)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentResult":
        if not data:
            return cls(page_count=0, pages=[])
        pages_list = [DocumentPage.from_dict(p) for p in data.get("pages", [])]
        return cls(
            page_count=data.get("page_count", 0),
            pages=pages_list,
            **{k: v for k, v in data.items() if k not in ("page_count", "pages")}
        )

    def __repr__(self) -> str:
        return f"<DocumentResult page_count={self.page_count} pages={len(self.pages)}>"


class ResumeResult:
    def __init__(self, name: str, total_experience_human: str, skills: list[str], **kwargs):
        self.name = name
        self.total_experience_human = total_experience_human
        self.skills = skills
        for k, v in kwargs.items():
            setattr(self, k, v)
        # The exact JSON body the API returned, before this class filled in any
        # default for a key the response omitted. Callers that need to persist
        # or display "what Veris said" must read this, not __dict__.
        self._raw_response: dict = {}

    @classmethod
    def from_dict(cls, data: dict) -> "ResumeResult":
        if not data:
            return cls(name="", total_experience_human="", skills=[])
        result = cls(
            name=data.get("name", ""),
            total_experience_human=data.get("total_experience_human", ""),
            skills=data.get("skills", []),
            **{k: v for k, v in data.items() if k not in ("name", "total_experience_human", "skills")}
        )
        result._raw_response = data
        return result

    def __repr__(self) -> str:
        return f"<ResumeResult name={self.name!r} total_experience_human={self.total_experience_human!r}>"
