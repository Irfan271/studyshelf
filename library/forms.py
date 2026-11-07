
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.utils.text import slugify

from .models import Member, Note, SubjectTag
from .validators import validate_note_file, validate_avatar


class RegisterForm(UserCreationForm):
    email = forms.EmailField()
    display_name = forms.CharField(max_length=90, required=True)

    class Meta:
        model = Member
        fields = (
            "username",
            "display_name",
            "email",
            "password1",
            "password2",
        )


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ("display_name", "email", "bio", "avatar")
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 3})
        }

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")

        if avatar:
            validate_avatar(avatar)

        return avatar


SUBJECT_OPTIONS = [
    "Programming",
    "Computer Science",
    "Information Technology",
    "Data Science",
    "Artificial Intelligence",
    "Mathematics",
    "Physics",
    "Chemistry",
    "Biology",
    "Education",
    "Engineering",
    "Electronics",
    "Business",
    "Economics",
    "Accounting",
    "Finance",
    "Management",
    "History",
    "Geography",
    "Political Science",
    "Psychology",
    "Sociology",
    "English",
    "Literature",
    "Languages",
    "Design",
    "Arts",
    "Medicine",
    "Law",
    "Environment",
    "Research",
    "Exam Preparation",
    "Other",
]


class MultiSubjectMixin:

    def clean_subjects(self):
        values = self.cleaned_data.get("subjects", [])

        if "Other" in values and len(values) > 1:
            raise forms.ValidationError(
                "Other must be selected on its own."
            )

        return values

    def clean(self):
        cleaned = super().clean()

        if (
            cleaned.get("subjects") == ["Other"]
            and not cleaned.get("other_subject", "").strip()
        ):
            self.add_error(
                "other_subject",
                "Please enter the other subject."
            )

        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk and not self.is_bound:

            existing = list(
                self.instance.subjects.values_list(
                    "name",
                    flat=True
                )
            )

            if not existing:
                existing = [
                    part.strip()
                    for part in (
                        self.instance.subject or ""
                    ).split(",")
                    if part.strip()
                ]

            known = [
                name for name in existing
                if name in SUBJECT_OPTIONS and name != "Other"
            ]

            custom = [
                name for name in existing
                if name not in SUBJECT_OPTIONS
            ]

            if custom and not known:
                self.initial["subjects"] = ["Other"]
                self.initial["other_subject"] = custom[0]

            elif known:
                self.initial["subjects"] = known

    def save_subjects(self, note):

        names = self.cleaned_data.get("subjects", [])

        if names == ["Other"]:
            names = [
                self.cleaned_data.get(
                    "other_subject",
                    ""
                ).strip()
            ]

        tags = []

        for name in names:

            if not name:
                continue

            base = slugify(name)[:60] or "subject"

            tag = SubjectTag.objects.filter(
                name=name
            ).first()

            if tag is None:

                slug = base
                suffix = 2

                while SubjectTag.objects.filter(
                    slug=slug
                ).exists():

                    slug = (
                        f"{base[:60-len(str(suffix))-1]}-{suffix}"
                    )

                    suffix += 1

                tag = SubjectTag.objects.create(
                    name=name,
                    slug=slug
                )

            tags.append(tag)

        note.subjects.set(tags)

        note.subject = ", ".join(
            tag.name for tag in tags
        )[:240]

        note.save(update_fields=["subject"])


class NoteForm(MultiSubjectMixin, forms.ModelForm):

    subjects = forms.MultipleChoiceField(
        choices=[(x, x) for x in SUBJECT_OPTIONS],
        required=True,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Choose one or more subjects. "
            "Select Other by itself and describe the subject below."
        ),
    )

    other_subject = forms.CharField(
        max_length=60,
        required=False,
        label="Other subject"
    )

    file = forms.FileField(
        validators=[validate_note_file],
        help_text=(
            "PDF, DOC/DOCX, PPT/PPTX, XLS/XLSX, CSV, "
            "TXT, RTF, ODT/ODS/ODP or MD. Maximum 10 MB."
        )
    )

    class Meta:
        model = Note

        fields = (
            "title",
            "description",
            "subjects",
            "file",
            "visibility",
        )

        widgets = {
            "description": forms.Textarea(
                attrs={"rows": 5}
            )
        }


class NoteUpdateForm(MultiSubjectMixin, forms.ModelForm):

    subjects = forms.MultipleChoiceField(
        choices=[(x, x) for x in SUBJECT_OPTIONS],
        required=True,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Choose one or more subjects. "
            "Select Other by itself and describe the subject below."
        ),
    )

    other_subject = forms.CharField(
        max_length=60,
        required=False,
        label="Other subject"
    )

    file = forms.FileField(
        required=False,
        validators=[validate_note_file]
    )

    class Meta:
        model = Note

        fields = (
            "title",
            "description",
            "subjects",
            "file",
            "visibility",
        )

        widgets = {
            "description": forms.Textarea(
                attrs={"rows": 5}
            )
        }
