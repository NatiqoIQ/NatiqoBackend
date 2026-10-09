"""The seed curriculum: six CEFR levels, declared as data.

Split from :mod:`scripts.seed_content` the same way :mod:`scripts.seed_personas` is split
from :mod:`scripts.seed`: this module is *content*, the other is *machinery*. Writing a
unit here should never mean reading the seeding code.

Two rules make the declaration safe to edit:

**A slug is permanent.** Every row's id is ``uuid5`` of its slug path, so a slug identifies
the row for the life of the database. Renaming a *title* is free; renaming a slug orphans
whatever progress is keyed on the old id. Slugs are therefore descriptive of the topic and
never of the wording.

**A slug is unique within its parent.** The four levels of the path — level, unit, lesson,
question — are joined with ``/`` to derive the id, so two questions sharing a slug inside
one lesson would silently be one row.

The content itself is British English for Iraqi learners, and the questions are answerable
without audio: a listening item quotes what the learner hears, so the catalogue can be
rendered by a client that has not shipped audio yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types import CEFRLevel, QuestionKind, SkillKind


@dataclass(frozen=True)
class SeedQuestion:
    slug: str
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: tuple[str, ...] = ()
    accepted_answers: tuple[str, ...] = ()
    correct_index: int | None = None
    explanation: str | None = None
    #: Optional pre-attempt visual aid. A question may illustrate the taught word or
    #: sentence without losing its explanation, which stays required.
    image_url: str | None = None
    points: int = 1


@dataclass(frozen=True)
class SeedLesson:
    slug: str
    title: str
    summary: str
    estimated_minutes: int
    questions: tuple[SeedQuestion, ...] = ()


@dataclass(frozen=True)
class SeedUnit:
    slug: str
    title: str
    description: str | None = None
    lessons: tuple[SeedLesson, ...] = ()


@dataclass(frozen=True)
class SeedLevel:
    slug: str
    code: CEFRLevel
    title: str
    description: str | None = None
    units: tuple[SeedUnit, ...] = ()


# ------------------------------------------------------------------------ A1
_A1: SeedLevel = SeedLevel(
    slug="a1-starter",
    code=CEFRLevel.A1,
    title="A1 Starter",
    description="Greetings, introductions and the words you need to get by.",
    units=(
        SeedUnit(
            slug="a1-greetings",
            title="Greetings and introductions",
            description="Saying hello, saying who you are, saying goodbye.",
            lessons=(
                SeedLesson(
                    slug="a1-greetings-hello",
                    title="Saying hello",
                    summary="Greet someone and respond to a greeting.",
                    estimated_minutes=10,
                    questions=(
                        SeedQuestion(
                            slug="a1-greetings-hello-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Someone says 'Hello!'. What do you say?",
                            choices=(
                                "Good morning!",
                                "Goodbye.",
                                "Thank you.",
                                "See you later.",
                            ),
                            correct_index=0,
                            explanation="'Hello' is answered with a greeting of your own.",
                            image_url="https://picsum.photos/seed/a1-greetings-hello-q1/640/360",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-hello-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete the phrase: 'Nice to ___ you.'",
                            accepted_answers=("meet",),
                            explanation="The idiom is 'nice to meet you'.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-hello-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.LISTENING,
                            prompt="'Good night' is a way to say goodbye in the evening.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It is both a greeting and a farewell.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a1-greetings-name",
                    title="Saying your name",
                    summary="Introduce yourself and spell a name.",
                    estimated_minutes=8,
                    questions=(
                        SeedQuestion(
                            slug="a1-greetings-name-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="How do you ask someone's name?",
                            choices=(
                                "What is your name?",
                                "How are you?",
                                "Where are you from?",
                                "How old are you?",
                            ),
                            correct_index=0,
                            explanation="'What is your name?' is the direct question.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-name-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'My name ___ Zainab.'",
                            accepted_answers=("is",),
                            explanation="Use 'is' with a singular subject.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-name-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which sentence introduces another person?",
                            choices=(
                                "This is my friend Ali.",
                                "I am fine, thank you.",
                                "It is half past two.",
                                "I live in Baghdad.",
                            ),
                            correct_index=0,
                            explanation="'This is...' presents someone to a listener.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-name-q4",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.PRONUNCIATION,
                            prompt="Which syllable is stressed in 'goodbye'?",
                            choices=(
                                "The second: good-BYE",
                                "The first: GOOD-bye",
                                "Both equally",
                                "Neither is stressed",
                            ),
                            correct_index=0,
                            explanation="The stress falls on the second syllable.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a1-greetings-goodbye",
                    title="Saying goodbye",
                    summary="Close a conversation and arrange to meet again.",
                    estimated_minutes=8,
                    questions=(
                        SeedQuestion(
                            slug="a1-greetings-goodbye-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which phrase says goodbye?",
                            choices=(
                                "See you tomorrow.",
                                "How do you do?",
                                "Nice to meet you.",
                                "What's your name?",
                            ),
                            correct_index=0,
                            explanation="It names the next time you will meet.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-goodbye-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete: '___ you later!'",
                            accepted_answers=("see",),
                            explanation="'See you later' is a fixed farewell.",
                        ),
                        SeedQuestion(
                            slug="a1-greetings-goodbye-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.GRAMMAR,
                            prompt="'Bye' is more formal than 'Goodbye'.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="'Bye' is the informal shortening.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="a1-numbers",
            title="Numbers and age",
            description="Counting, and answering the question everybody asks.",
            lessons=(
                SeedLesson(
                    slug="a1-numbers-age",
                    title="How old are you?",
                    summary="Ask and answer with a number.",
                    estimated_minutes=7,
                    questions=(
                        SeedQuestion(
                            slug="a1-numbers-age-q1",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Write the number: 'twelve'.",
                            accepted_answers=("12",),
                            explanation="Marking ignores case and surrounding spaces.",
                        ),
                        SeedQuestion(
                            slug="a1-numbers-age-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete the question: 'How old ___ you?'",
                            accepted_answers=("are",),
                            explanation="'How old are you?'",
                        ),
                        SeedQuestion(
                            slug="a1-numbers-age-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.LISTENING,
                            prompt="You hear: 'I'm twenty-one.' How old is the speaker?",
                            choices=("21", "12", "20", "31"),
                            correct_index=0,
                            explanation="'Twenty-one' is 21; the '-one' is the unit.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a1-numbers-time",
                    title="Telling the time",
                    summary="Ask for the time and read a clock in English.",
                    estimated_minutes=9,
                    questions=(
                        SeedQuestion(
                            slug="a1-numbers-time-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="How do you ask the time politely?",
                            choices=(
                                "What time is it, please?",
                                "What time you are?",
                                "How much time?",
                                "Which hour it is?",
                            ),
                            correct_index=0,
                            explanation="'What time is it?' is the fixed question.",
                        ),
                        SeedQuestion(
                            slug="a1-numbers-time-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete: 'It is half ___ six.' (6:30)",
                            accepted_answers=("past",),
                            explanation="Half past six is 6:30.",
                        ),
                        SeedQuestion(
                            slug="a1-numbers-time-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.LISTENING,
                            prompt="'Quarter to nine' means 8:45.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="A quarter of an hour before nine.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="a1-family",
            title="Family and people",
            description="The words for relatives, and whose they are.",
            lessons=(
                SeedLesson(
                    slug="a1-family-people",
                    title="Talking about your family",
                    summary="Name the people in a family.",
                    estimated_minutes=9,
                    questions=(
                        SeedQuestion(
                            slug="a1-family-people-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Your mother's brother is your ___.",
                            choices=("uncle", "cousin", "nephew", "grandfather"),
                            correct_index=0,
                            explanation="A parent's brother is an uncle.",
                            image_url="https://picsum.photos/seed/a1-family-people-q1/640/360",
                        ),
                        SeedQuestion(
                            slug="a1-family-people-q2",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which word means 'a child of your aunt'?",
                            choices=("cousin", "sister", "aunt", "niece"),
                            correct_index=0,
                            explanation="Cousins are the children of your aunts and uncles.",
                        ),
                        SeedQuestion(
                            slug="a1-family-people-q3",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="My father and mother are my ___.",
                            accepted_answers=("parents",),
                            explanation="Two parents; the singular is 'parent'.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a1-family-possessive",
                    title="My, your, his, her",
                    summary="Choose the possessive word that agrees with the owner.",
                    estimated_minutes=10,
                    questions=(
                        SeedQuestion(
                            slug="a1-family-possessive-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Zainab is my sister. ___ name is Zainab.",
                            choices=("Her", "His", "Their", "Your"),
                            correct_index=0,
                            explanation="Zainab is female and singular.",
                        ),
                        SeedQuestion(
                            slug="a1-family-possessive-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="This is Ali and this is ___ brother.",
                            accepted_answers=("his",),
                            explanation="Ali is male and singular.",
                        ),
                        SeedQuestion(
                            slug="a1-family-possessive-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.GRAMMAR,
                            prompt="We use 'their' for more than one person.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="'Their' belongs to a plural or unknown-gender owner.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------------------------ A2
_A2: SeedLevel = SeedLevel(
    slug="a2-daily-life",
    code=CEFRLevel.A2,
    title="A2 Daily life",
    description="Describing routines, plans and the place you live.",
    units=(
        SeedUnit(
            slug="a2-routines",
            title="Daily routines",
            description="Talking about what you do every day.",
            lessons=(
                SeedLesson(
                    slug="a2-routines-morning",
                    title="The morning routine",
                    summary="Describe a sequence of everyday actions.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="a2-routines-morning-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which activity happens first in a typical morning?",
                            choices=(
                                "Having breakfast",
                                "Going to work",
                                "Watching television",
                                "Going to bed",
                            ),
                            correct_index=0,
                            explanation="Breakfast comes before the rest of the day.",
                        ),
                        SeedQuestion(
                            slug="a2-routines-morning-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'I usually ___ up at seven.'",
                            accepted_answers=("get",),
                            explanation="The phrasal verb is 'get up'.",
                        ),
                        SeedQuestion(
                            slug="a2-routines-morning-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which sentence describes a habit?",
                            choices=(
                                "She has a shower every morning.",
                                "She is having a shower now.",
                                "She had a shower yesterday.",
                                "She will have a shower later.",
                            ),
                            correct_index=0,
                            explanation="The present simple with 'every morning' marks a habit.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a2-routines-week",
                    title="Days and the week",
                    summary="Place events on the days and times of a week.",
                    estimated_minutes=10,
                    questions=(
                        SeedQuestion(
                            slug="a2-routines-week-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which preposition is correct? 'I work ___ Sunday.'",
                            choices=("on", "in", "at", "to"),
                            correct_index=0,
                            explanation="Days take 'on': on Sunday, on Friday.",
                        ),
                        SeedQuestion(
                            slug="a2-routines-week-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="The day between Tuesday and Thursday is ___.",
                            accepted_answers=("wednesday",),
                            explanation="The week runs Tuesday, Wednesday, Thursday.",
                        ),
                        SeedQuestion(
                            slug="a2-routines-week-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which pair are both weekend days?",
                            choices=(
                                "Saturday and Sunday",
                                "Monday and Tuesday",
                                "Friday and Monday",
                                "Wednesday and Thursday",
                            ),
                            correct_index=0,
                            explanation="In most English-speaking countries the weekend is Saturday and Sunday.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="a2-home",
            title="Home and city",
            description="The place you live, and how to describe it.",
            lessons=(
                SeedLesson(
                    slug="a2-home-places",
                    title="Places in a city",
                    summary="Name the shops and services around you.",
                    estimated_minutes=10,
                    questions=(
                        SeedQuestion(
                            slug="a2-home-places-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Where do you go to send a letter?",
                            choices=("The post office", "The pharmacy", "The bakery", "The bank"),
                            correct_index=0,
                            explanation="Post offices handle letters and parcels.",
                        ),
                        SeedQuestion(
                            slug="a2-home-places-q2",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Where do you buy medicine?",
                            choices=("The pharmacy", "The library", "The market", "The garage"),
                            correct_index=0,
                            explanation="A pharmacy, also called a chemist in British English.",
                        ),
                        SeedQuestion(
                            slug="a2-home-places-q3",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="You keep your money at the ___.",
                            accepted_answers=("bank",),
                            explanation="Banks hold accounts and change money.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a2-home-there-is",
                    title="There is / there are",
                    summary="Say what exists, and what does not.",
                    estimated_minutes=10,
                    questions=(
                        SeedQuestion(
                            slug="a2-home-there-is-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="___ three bedrooms in our flat.",
                            choices=("There are", "There is", "It is", "They is"),
                            correct_index=0,
                            explanation="'There are' agrees with a plural noun.",
                        ),
                        SeedQuestion(
                            slug="a2-home-there-is-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: '___ is a small garden behind the house.'",
                            accepted_answers=("there",),
                            explanation="'There is' introduces one thing.",
                        ),
                        SeedQuestion(
                            slug="a2-home-there-is-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.GRAMMAR,
                            prompt="'There is two chairs' is correct English.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="Two chairs are plural, so it needs 'there are'.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="a2-shopping",
            title="Shopping and prices",
            description="Buying things, asking the price, and paying.",
            lessons=(
                SeedLesson(
                    slug="a2-shopping-market",
                    title="At the market",
                    summary="Ask for what you want and say how much you need.",
                    estimated_minutes=11,
                    questions=(
                        SeedQuestion(
                            slug="a2-shopping-market-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="How do you ask politely for an item in a shop?",
                            choices=(
                                "Could I have a kilo of tomatoes, please?",
                                "Give me tomatoes.",
                                "I want that tomatoes.",
                                "Tomatoes me please.",
                            ),
                            correct_index=0,
                            explanation="'Could I have..., please?' is the polite request.",
                        ),
                        SeedQuestion(
                            slug="a2-shopping-market-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'How ___ does it cost?'",
                            accepted_answers=("much",),
                            explanation="Price takes 'how much'; countable quantity takes 'how many'.",
                        ),
                        SeedQuestion(
                            slug="a2-shopping-market-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="What does a shop assistant say when you pay in cash?",
                            choices=(
                                "Here is your change.",
                                "Here is your receipt number.",
                                "Your order is delivered.",
                                "Please try it on.",
                            ),
                            correct_index=0,
                            explanation="Change is the money returned when you overpay.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="a2-shopping-money",
                    title="Reading a price",
                    summary="Understand written prices and quantities.",
                    estimated_minutes=9,
                    questions=(
                        SeedQuestion(
                            slug="a2-shopping-money-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="A label reads '£4.50'. Which is the price?",
                            choices=(
                                "Four pounds fifty",
                                "Four hundred and fifty pounds",
                                "Forty-five pounds",
                                "Four pounds five",
                            ),
                            correct_index=0,
                            explanation="Two decimal places are pence: four pounds and fifty pence.",
                        ),
                        SeedQuestion(
                            slug="a2-shopping-money-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Half of ten kilos is ___ kilos.",
                            accepted_answers=("five", "5"),
                            explanation="Half of ten is five.",
                        ),
                        SeedQuestion(
                            slug="a2-shopping-money-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.VOCABULARY,
                            prompt="A receipt is the paper that proves you paid.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="A receipt is your proof of purchase.",
                        ),
                        SeedQuestion(
                            slug="a2-shopping-money-q4",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.PRONUNCIATION,
                            prompt="Which word rhymes with 'two'?",
                            choices=("blue", "go", "now", "how"),
                            correct_index=0,
                            explanation="'Two' and 'blue' share the long /u:/ sound.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------------------------ B1
_B1: SeedLevel = SeedLevel(
    slug="b1-independence",
    code=CEFRLevel.B1,
    title="B1 Independence",
    description="Handling everyday life on your own: work, travel and health.",
    units=(
        SeedUnit(
            slug="b1-work",
            title="Work and study",
            description="Describing what you do, and applying for something new.",
            lessons=(
                SeedLesson(
                    slug="b1-work-jobs",
                    title="Jobs and duties",
                    summary="Say what a job involves and what it requires.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b1-work-jobs-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="A person who keeps and examines financial records is an ___.",
                            choices=("accountant", "architect", "electrician", "translator"),
                            correct_index=0,
                            explanation="An accountant works with accounts.",
                        ),
                        SeedQuestion(
                            slug="b1-work-jobs-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'She has been working here ___ 2019.'",
                            accepted_answers=("since",),
                            explanation="'Since' takes a starting point; 'for' takes a duration.",
                        ),
                        SeedQuestion(
                            slug="b1-work-jobs-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="'Shift work' means working the same hours every weekday.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="A shift is one block of hours, and shifts usually rotate.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b1-work-interviews",
                    title="A job interview",
                    summary="Answer the questions an interviewer actually asks.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b1-work-interviews-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="The advertisement says 'previous experience essential'. What does that mean?",
                            choices=(
                                "You must have done this kind of work before.",
                                "Experience is helpful but not needed.",
                                "You will be trained from the beginning.",
                                "You must have a university degree.",
                            ),
                            correct_index=0,
                            explanation="'Essential' is a requirement, not a preference.",
                        ),
                        SeedQuestion(
                            slug="b1-work-interviews-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'I am writing to apply ___ the position of teacher.'",
                            accepted_answers=("for",),
                            explanation="One applies *for* a job.",
                        ),
                        SeedQuestion(
                            slug="b1-work-interviews-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which opening is appropriate in a formal application letter?",
                            choices=(
                                "Dear Sir or Madam,",
                                "Hi there,",
                                "Hey,",
                                "To whom it may concern lol,",
                            ),
                            correct_index=0,
                            explanation="'Dear Sir or Madam' is the standard formal opening when the name is unknown.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="b1-travel",
            title="Travel",
            description="Getting through an airport and checking into a hotel.",
            lessons=(
                SeedLesson(
                    slug="b1-travel-airport",
                    title="At the airport",
                    summary="Follow the stages of a flight from check-in to boarding.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="b1-travel-airport-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Where do you collect your suitcase after landing?",
                            choices=(
                                "Baggage reclaim",
                                "Check-in desk",
                                "Departure gate",
                                "Security control",
                            ),
                            correct_index=0,
                            explanation="Baggage reclaim is where luggage leaves the aircraft.",
                            image_url="https://picsum.photos/seed/b1-travel-airport-q1/640/360",
                        ),
                        SeedQuestion(
                            slug="b1-travel-airport-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete: 'Your flight has been ___ by three hours.' (made later)",
                            accepted_answers=("delayed",),
                            explanation="A delay moves the departure later.",
                        ),
                        SeedQuestion(
                            slug="b1-travel-airport-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="A boarding pass shows your seat number.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="The seat and the gate are both printed on it.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b1-travel-hotel",
                    title="At the hotel",
                    summary="Check in, ask questions, and complain politely.",
                    estimated_minutes=11,
                    questions=(
                        SeedQuestion(
                            slug="b1-travel-hotel-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which is the most polite complaint?",
                            choices=(
                                "I'm afraid the room is rather noisy.",
                                "Your hotel is terrible.",
                                "The room noise very bad.",
                                "I want another room now.",
                            ),
                            correct_index=0,
                            explanation="'I'm afraid...' softens a complaint without hiding it.",
                        ),
                        SeedQuestion(
                            slug="b1-travel-hotel-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'Could you ___ me a wake-up call at six?'",
                            accepted_answers=("give", "arrange", "book"),
                            explanation="'Give' and 'arrange' both work in the request.",
                        ),
                        SeedQuestion(
                            slug="b1-travel-hotel-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="What does 'en suite' mean in a hotel listing?",
                            choices=(
                                "The room has its own bathroom.",
                                "Breakfast is included.",
                                "The room faces the street.",
                                "The room sleeps four people.",
                            ),
                            correct_index=0,
                            explanation="'En suite' means the bathroom is in the room.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="b1-health",
            title="Health",
            description="Describing symptoms and understanding advice.",
            lessons=(
                SeedLesson(
                    slug="b1-health-doctor",
                    title="At the doctor's",
                    summary="Explain what is wrong and how long it has lasted.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b1-health-doctor-q1",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'I have had this cough ___ three days.'",
                            accepted_answers=("for",),
                            explanation="'For' takes a duration.",
                        ),
                        SeedQuestion(
                            slug="b1-health-doctor-q2",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="A doctor writes you a prescription. What do you do with it?",
                            choices=(
                                "Take it to a pharmacy to get the medicine.",
                                "Send it to your employer.",
                                "Keep it as proof of payment.",
                                "Give it to the receptionist at the airport.",
                            ),
                            correct_index=0,
                            explanation="A prescription authorises a pharmacist to dispense the medicine.",
                        ),
                        SeedQuestion(
                            slug="b1-health-doctor-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.VOCABULARY,
                            prompt="'I feel under the weather' means I feel unwell.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It is an idiom for being slightly ill.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b1-health-advice",
                    title="Giving advice",
                    summary="Recommend, warn and reassure.",
                    estimated_minutes=11,
                    questions=(
                        SeedQuestion(
                            slug="b1-health-advice-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which sentence gives advice?",
                            choices=(
                                "You should rest for a couple of days.",
                                "You rested for a couple of days.",
                                "You are resting for a couple of days.",
                                "You will have rested for a couple of days.",
                            ),
                            correct_index=0,
                            explanation="'Should' is the modal for advice.",
                        ),
                        SeedQuestion(
                            slug="b1-health-advice-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'If I ___ you, I would see a doctor.'",
                            accepted_answers=("were", "was"),
                            explanation="The second conditional takes 'were' in the if-clause.",
                        ),
                        SeedQuestion(
                            slug="b1-health-advice-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which is the strongest warning?",
                            choices=(
                                "You must not lift anything heavy.",
                                "You could lift something light.",
                                "You might prefer to rest.",
                                "You may want to sit down.",
                            ),
                            correct_index=0,
                            explanation="'Must not' forbids; the others only suggest.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------------------------ B2
_B2: SeedLevel = SeedLevel(
    slug="b2-opinions",
    code=CEFRLevel.B2,
    title="B2 Opinions",
    description="Argue a position, read the news, and talk about the world.",
    units=(
        SeedUnit(
            slug="b2-opinions",
            title="Opinions",
            description="Taking a position and defending it in writing.",
            lessons=(
                SeedLesson(
                    slug="b2-opinions-agreeing",
                    title="Agreeing and disagreeing",
                    summary="Respond to a claim without sounding either vague or rude.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b2-opinions-agreeing-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which sentence disagrees most tentatively?",
                            choices=(
                                "I'm not sure I'd go that far.",
                                "That is completely wrong.",
                                "No, that's nonsense.",
                                "You are mistaken.",
                            ),
                            correct_index=0,
                            explanation="It signals limited disagreement rather than a verdict.",
                        ),
                        SeedQuestion(
                            slug="b2-opinions-agreeing-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'I agree ___ you up to a point.'",
                            accepted_answers=("with",),
                            explanation="One agrees *with* a person.",
                        ),
                        SeedQuestion(
                            slug="b2-opinions-agreeing-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.GRAMMAR,
                            prompt="'I see your point, but...' introduces a concession.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It accepts the other view before disagreeing.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b2-opinions-paragraph",
                    title="Writing an argument",
                    summary="Build a paragraph with a claim, evidence and a consequence.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="b2-opinions-paragraph-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which connector introduces a result?",
                            choices=("Consequently", "However", "Whereas", "Although"),
                            correct_index=0,
                            explanation="'Consequently' marks a consequence.",
                        ),
                        SeedQuestion(
                            slug="b2-opinions-paragraph-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.WRITING,
                            prompt="Complete: '___ the cost, the plan is worth trying.' (despite)",
                            accepted_answers=("despite", "notwithstanding"),
                            explanation="'Despite' takes a noun phrase.",
                        ),
                        SeedQuestion(
                            slug="b2-opinions-paragraph-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="Which sentence is a claim rather than a fact?",
                            choices=(
                                "City centres are better without cars.",
                                "The city centre has 4,000 parking spaces.",
                                "The council met on Tuesday.",
                                "The scheme cost two million dinars.",
                            ),
                            correct_index=0,
                            explanation="It expresses a judgement that could be argued either way.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="b2-media",
            title="News and media",
            description="Reading headlines and reporting what others said.",
            lessons=(
                SeedLesson(
                    slug="b2-media-headlines",
                    title="Reading headlines",
                    summary="Decode the compressed grammar of a headline.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b2-media-headlines-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="The headline reads 'Ministers clash over budget'. Who is involved?",
                            choices=(
                                "Government ministers",
                                "Bank managers",
                                "Teachers",
                                "Doctors",
                            ),
                            correct_index=0,
                            explanation="'Ministers' here are members of a government.",
                        ),
                        SeedQuestion(
                            slug="b2-media-headlines-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.READING,
                            prompt="Headlines often drop the verb 'to be'. Complete: 'Three injured ___ crash.'",
                            accepted_answers=("in",),
                            explanation="'In' places the event without a full sentence.",
                        ),
                        SeedQuestion(
                            slug="b2-media-headlines-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="A tabloid headline is usually more restrained than a broadsheet one.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="Tabloids are typically more sensational.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b2-media-passive",
                    title="The passive voice",
                    summary="Report an action when the doer is unknown or unimportant.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="b2-media-passive-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which is the passive form of 'They built the bridge in 1998'?",
                            choices=(
                                "The bridge was built in 1998.",
                                "The bridge built in 1998.",
                                "The bridge is building in 1998.",
                                "They were building the bridge in 1998.",
                            ),
                            correct_index=0,
                            explanation="'Was built' is past simple passive.",
                        ),
                        SeedQuestion(
                            slug="b2-media-passive-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'The results ___ been published.'",
                            accepted_answers=("have",),
                            explanation="Present perfect passive: have been published.",
                        ),
                        SeedQuestion(
                            slug="b2-media-passive-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Why does a news report prefer the passive here: 'Two people were arrested'?",
                            choices=(
                                "The identity of the officer is irrelevant.",
                                "The reporter does not know the grammar.",
                                "It makes the sentence shorter than any alternative.",
                                "It hides that no arrest happened.",
                            ),
                            correct_index=0,
                            explanation="The passive foregrounds what happened to whom.",
                        ),
                        SeedQuestion(
                            slug="b2-media-passive-q4",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.PRONUNCIATION,
                            prompt="Which letter is silent in the word 'doubt'?",
                            choices=("b", "d", "o", "t"),
                            correct_index=0,
                            explanation="'Doubt' is pronounced like 'dout'; the 'b' is not sounded.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="b2-environment",
            title="Environment and society",
            description="Naming a problem and proposing a solution.",
            lessons=(
                SeedLesson(
                    slug="b2-environment-problems",
                    title="Describing problems",
                    summary="Explain a cause and say how serious it is.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b2-environment-problems-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which word means 'damage caused to water or air'?",
                            choices=("Pollution", "Population", "Proportion", "Production"),
                            correct_index=0,
                            explanation="Pollution is contamination of the environment.",
                        ),
                        SeedQuestion(
                            slug="b2-environment-problems-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'Traffic is the main ___ of air pollution in the city.' (cause)",
                            accepted_answers=("cause", "source"),
                            explanation="A cause or a source brings the problem about.",
                        ),
                        SeedQuestion(
                            slug="b2-environment-problems-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="'A drop in the ocean' means an effect that is large enough to matter.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="It means the opposite: too small to make a difference.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="b2-environment-solutions",
                    title="Suggesting solutions",
                    summary="Propose, weigh and recommend an action.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="b2-environment-solutions-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which sentence proposes a solution most formally?",
                            choices=(
                                "One option would be to subsidise public transport.",
                                "Just ban all the cars.",
                                "Why not stop driving?",
                                "They should do something about it.",
                            ),
                            correct_index=0,
                            explanation="'One option would be...' advances a proposal without demanding agreement.",
                        ),
                        SeedQuestion(
                            slug="b2-environment-solutions-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'This approach is likely ___ reduce congestion.'",
                            accepted_answers=("to",),
                            explanation="'Likely' is followed by an infinitive with 'to'.",
                        ),
                        SeedQuestion(
                            slug="b2-environment-solutions-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which sentence weighs a drawback?",
                            choices=(
                                "The scheme is effective, although it is expensive.",
                                "The scheme is effective and it is expensive.",
                                "The scheme is effective because it is expensive.",
                                "The scheme is effective, therefore it is expensive.",
                            ),
                            correct_index=0,
                            explanation="'Although' concedes the drawback without abandoning the claim.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------------------------ C1
_C1: SeedLevel = SeedLevel(
    slug="c1-fluency",
    code=CEFRLevel.C1,
    title="C1 Fluency",
    description="Nuance, register and idiom for fluent discussion.",
    units=(
        SeedUnit(
            slug="c1-nuance",
            title="Nuance",
            description="Saying what you mean precisely, and to whom.",
            lessons=(
                SeedLesson(
                    slug="c1-nuance-idioms",
                    title="Everyday idioms",
                    summary="Use idioms that a fluent speaker would actually use.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c1-nuance-idioms-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="'We'll cross that bridge when we come to it' means ___.",
                            choices=(
                                "We will deal with the problem if it arises.",
                                "We have already solved the problem.",
                                "We should avoid the problem forever.",
                                "We are building something together.",
                            ),
                            correct_index=0,
                            explanation="It defers a decision until it is actually needed.",
                        ),
                        SeedQuestion(
                            slug="c1-nuance-idioms-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete the idiom: 'to be on the same ___' (to agree).",
                            accepted_answers=("page", "wavelength"),
                            explanation="Both mean sharing an understanding.",
                        ),
                        SeedQuestion(
                            slug="c1-nuance-idioms-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.VOCABULARY,
                            prompt="'To pull someone's leg' means to deceive them seriously.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="It means to tease them, with no harm intended.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c1-nuance-register",
                    title="Formal and informal register",
                    summary="Match the wording to the situation.",
                    estimated_minutes=14,
                    questions=(
                        SeedQuestion(
                            slug="c1-nuance-register-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which is the most formal way to say 'I want to know more'?",
                            choices=(
                                "I would appreciate further details.",
                                "I wanna know more.",
                                "Tell me the rest.",
                                "I'd love more info.",
                            ),
                            correct_index=0,
                            explanation="The passive 'would appreciate' is standard in formal correspondence.",
                        ),
                        SeedQuestion(
                            slug="c1-nuance-register-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.WRITING,
                            prompt="Formal alternative to 'get in touch': '___ contact with us.'",
                            accepted_answers=("make", "establish"),
                            explanation="'Make contact' is the neutral formal collocation.",
                        ),
                        SeedQuestion(
                            slug="c1-nuance-register-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="'Your application has not been successful' is ___.",
                            choices=(
                                "formal and impersonal",
                                "informal and friendly",
                                "sarcastic",
                                "grammatically incorrect",
                            ),
                            correct_index=0,
                            explanation="Institutions prefer the passive when delivering a rejection.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="c1-argument",
            title="Argument",
            description="Linking ideas and staying appropriately cautious.",
            lessons=(
                SeedLesson(
                    slug="c1-argument-linking",
                    title="Linking an argument",
                    summary="Move from one point to the next without losing the reader.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c1-argument-linking-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which connector introduces a counter-argument to what you just said?",
                            choices=(
                                "Nevertheless",
                                "Similarly",
                                "Moreover",
                                "Therefore",
                            ),
                            correct_index=0,
                            explanation="'Nevertheless' concedes a contrast.",
                        ),
                        SeedQuestion(
                            slug="c1-argument-linking-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.WRITING,
                            prompt="Complete: 'The evidence is thin; ___, the conclusion may still hold.' (however)",
                            accepted_answers=("however", "nevertheless", "nonetheless"),
                            explanation="All three mark a concession after a negative point.",
                        ),
                        SeedQuestion(
                            slug="c1-argument-linking-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.WRITING,
                            prompt="'In other words' signals that a restatement follows.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It reformulates the previous point more clearly.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c1-argument-hedging",
                    title="Hedging and caution",
                    summary="Say something without overclaiming.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="c1-argument-hedging-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which sentence is hedged?",
                            choices=(
                                "The policy appears to have reduced traffic.",
                                "The policy reduced traffic.",
                                "The policy cut traffic by half.",
                                "The policy is a total success.",
                            ),
                            correct_index=0,
                            explanation="'Appears to' limits the claim to the available evidence.",
                        ),
                        SeedQuestion(
                            slug="c1-argument-hedging-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'The results ___ suggest a link, but not conclusively.'",
                            accepted_answers=("may", "might", "could"),
                            explanation="All three modals withhold certainty.",
                        ),
                        SeedQuestion(
                            slug="c1-argument-hedging-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="'This is arguably the best solution' means ___.",
                            choices=(
                                "There is a reasonable case for it.",
                                "It is certainly the best solution.",
                                "Nobody believes it is the best solution.",
                                "It is the only solution.",
                            ),
                            correct_index=0,
                            explanation="'Arguably' invites the possibility of disagreement.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="c1-culture",
            title="Culture",
            description="Discussing books, film and humour.",
            lessons=(
                SeedLesson(
                    slug="c1-culture-literature",
                    title="Talking about literature",
                    summary="Describe a plot, a character and a theme.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c1-culture-literature-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="The main character of a novel is the ___.",
                            choices=("protagonist", "antagonist", "narrator", "publisher"),
                            correct_index=0,
                            explanation="The protagonist drives the story; the antagonist opposes them.",
                        ),
                        SeedQuestion(
                            slug="c1-culture-literature-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="A story with a hidden meaning that teaches a lesson is a ___.",
                            accepted_answers=("fable", "parable", "allegory"),
                            explanation="All three carry a moral or second meaning.",
                        ),
                        SeedQuestion(
                            slug="c1-culture-literature-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="A 'plot twist' is a change the reader saw coming from page one.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="The point of a twist is that it overturns the reader's expectation.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c1-culture-humour",
                    title="Humour and irony",
                    summary="Recognise when the surface meaning is not the real one.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="c1-culture-humour-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="It is pouring with rain and someone says 'Lovely weather!'. They are ___.",
                            choices=(
                                "being ironic",
                                "being sincere",
                                "giving a forecast",
                                "asking a question",
                            ),
                            correct_index=0,
                            explanation="The statement is the opposite of the visible facts.",
                        ),
                        SeedQuestion(
                            slug="c1-culture-humour-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="A joke that plays on a word's double meaning is a ___. (play on words)",
                            accepted_answers=("pun",),
                            explanation="A pun exploits two senses of one word or two similar sounds.",
                        ),
                        SeedQuestion(
                            slug="c1-culture-humour-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.LISTENING,
                            prompt="You hear 'That went well' immediately after a disaster. The tone is ___.",
                            choices=(
                                "sarcastic",
                                "genuinely pleased",
                                "formal",
                                "apologetic",
                            ),
                            correct_index=0,
                            explanation="The timing, not the words, carries the mockery.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

# ------------------------------------------------------------------------ C2
_C2: SeedLevel = SeedLevel(
    slug="c2-mastery",
    code=CEFRLevel.C2,
    title="C2 Mastery",
    description="Near-native precision: style, ambiguity and subtle shifts of tone.",
    units=(
        SeedUnit(
            slug="c2-style",
            title="Style",
            description="Choosing between words that mean almost the same thing.",
            lessons=(
                SeedLesson(
                    slug="c2-style-connotation",
                    title="Connotation and precision",
                    summary="Weigh what a word implies, not only what it denotes.",
                    estimated_minutes=14,
                    questions=(
                        SeedQuestion(
                            slug="c2-style-connotation-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="Which word is the most negative?",
                            choices=("stubborn", "determined", "resolute", "persistent"),
                            correct_index=0,
                            explanation="The others approve of the same trait; 'stubborn' does not.",
                        ),
                        SeedQuestion(
                            slug="c2-style-connotation-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Neutral word for a person who studies a lot: '___'. (approving: 'studious')",
                            accepted_answers=("diligent", "hardworking"),
                            explanation="These describe effort without the disapproval of 'nerdy'.",
                        ),
                        SeedQuestion(
                            slug="c2-style-connotation-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="A report calls a decision 'hasty'. The writer believes it was ___.",
                            choices=(
                                "made too quickly",
                                "made bravely",
                                "made carefully",
                                "made by committee",
                            ),
                            correct_index=0,
                            explanation="'Hasty' carries the criticism of insufficient thought.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c2-style-inversion",
                    title="Emphasis and inversion",
                    summary="Rearrange a sentence to put weight where you want it.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c2-style-inversion-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.GRAMMAR,
                            prompt="Which sentence uses inversion correctly for emphasis?",
                            choices=(
                                "Never have I seen such a thing.",
                                "Never I have seen such a thing.",
                                "I never have seen such a thing.",
                                "Never seen I have such a thing.",
                            ),
                            correct_index=0,
                            explanation="A fronted negative adverb inverts subject and auxiliary.",
                        ),
                        SeedQuestion(
                            slug="c2-style-inversion-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.GRAMMAR,
                            prompt="Complete: 'Only later ___ we realise the cost.' (inverted auxiliary)",
                            accepted_answers=("did",),
                            explanation="'Only later' fronts the adverbial, so the auxiliary follows it.",
                        ),
                        SeedQuestion(
                            slug="c2-style-inversion-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.WRITING,
                            prompt="Inversion makes a formal text sound more emphatic.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It draws attention to the fronted element.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="c2-discourse",
            title="Discourse",
            description="Whole texts: how they hold together and what they imply.",
            lessons=(
                SeedLesson(
                    slug="c2-discourse-cohesion",
                    title="Cohesion across a text",
                    summary="Keep a long text pointing in one direction.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c2-discourse-cohesion-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.WRITING,
                            prompt="Which device refers back to something mentioned earlier?",
                            choices=(
                                "The former",
                                "Furthermore",
                                "Meanwhile",
                                "In addition",
                            ),
                            correct_index=0,
                            explanation="'The former' and 'the latter' pick out one of two earlier items.",
                        ),
                        SeedQuestion(
                            slug="c2-discourse-cohesion-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.WRITING,
                            prompt="Complete: 'The proposal was rejected. ___, the committee agreed to review it.' (contrast)",
                            accepted_answers=("however", "nevertheless", "nonetheless", "still"),
                            explanation="Each marks a turn against the previous sentence.",
                        ),
                        SeedQuestion(
                            slug="c2-discourse-cohesion-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.READING,
                            prompt="Repeating a key noun exactly is generally preferred to using a pronoun in a long text.",
                            choices=("True", "False"),
                            correct_index=1,
                            explanation="Writers vary the reference; a pronoun or synonym usually reads better than repetition.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c2-discourse-tone",
                    title="Tone and implication",
                    summary="Work out what a text assumes and what it leaves unsaid.",
                    estimated_minutes=14,
                    questions=(
                        SeedQuestion(
                            slug="c2-discourse-tone-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="'It is hardly surprising that the plan failed.' The writer ___.",
                            choices=(
                                "says the failure was predictable",
                                "is surprised by the failure",
                                "believes the plan will succeed",
                                "refuses to comment",
                            ),
                            correct_index=0,
                            explanation="'Hardly surprising' states that nobody should be taken aback.",
                        ),
                        SeedQuestion(
                            slug="c2-discourse-tone-q2",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="Which sentence implies the speaker doubts the claim?",
                            choices=(
                                "He claims to have finished the report.",
                                "He has finished the report.",
                                "He finished the report yesterday.",
                                "He told me the report is finished.",
                            ),
                            correct_index=0,
                            explanation="Reporting a claim with 'claims' distances the speaker from its truth.",
                        ),
                        SeedQuestion(
                            slug="c2-discourse-tone-q3",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.WRITING,
                            prompt="Complete: 'The memo stops ___ of criticising the decision.' (without *short*)",
                            accepted_answers=("short",),
                            explanation="'To stop short of' means to go almost but not quite as far.",
                        ),
                    ),
                ),
            ),
        ),
        SeedUnit(
            slug="c2-idiom",
            title="Idiom",
            description="Phrasal verbs and figurative language at speed.",
            lessons=(
                SeedLesson(
                    slug="c2-idiom-phrasal",
                    title="Advanced phrasal verbs",
                    summary="Tell apart the phrasal verbs that trip people up.",
                    estimated_minutes=13,
                    questions=(
                        SeedQuestion(
                            slug="c2-idiom-phrasal-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="'We need to iron out the details' means ___.",
                            choices=(
                                "to resolve the remaining problems",
                                "to remove the details entirely",
                                "to write them down carefully",
                                "to postpone the discussion",
                            ),
                            correct_index=0,
                            explanation="'Iron out' means to smooth away difficulties.",
                        ),
                        SeedQuestion(
                            slug="c2-idiom-phrasal-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="Complete: 'The deal fell ___ at the last minute.' (failed)",
                            accepted_answers=("through",),
                            explanation="'Fall through' means to fail to happen.",
                        ),
                        SeedQuestion(
                            slug="c2-idiom-phrasal-q3",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.VOCABULARY,
                            prompt="'She put the meeting off' means she ___.",
                            choices=(
                                "postponed it",
                                "cancelled it",
                                "attended it",
                                "forgot it",
                            ),
                            correct_index=0,
                            explanation="'Put off' shifts it later; cancelling would be 'call off'.",
                        ),
                    ),
                ),
                SeedLesson(
                    slug="c2-idiom-metaphor",
                    title="Metaphor and imagery",
                    summary="Follow a comparison through a whole text.",
                    estimated_minutes=12,
                    questions=(
                        SeedQuestion(
                            slug="c2-idiom-metaphor-q1",
                            kind=QuestionKind.MULTIPLE_CHOICE,
                            skill=SkillKind.READING,
                            prompt="'The city is a sleeping giant' is ___.",
                            choices=(
                                "a metaphor",
                                "a simile",
                                "hyperbole",
                                "onomatopoeia",
                            ),
                            correct_index=0,
                            explanation="It asserts the comparison without 'like' or 'as'.",
                        ),
                        SeedQuestion(
                            slug="c2-idiom-metaphor-q2",
                            kind=QuestionKind.FILL_BLANK,
                            skill=SkillKind.VOCABULARY,
                            prompt="'As quiet as a ___' completes the simile. (small rodent)",
                            accepted_answers=("mouse",),
                            explanation="The fixed simile is 'as quiet as a mouse'.",
                        ),
                        SeedQuestion(
                            slug="c2-idiom-metaphor-q3",
                            kind=QuestionKind.TRUE_FALSE,
                            skill=SkillKind.WRITING,
                            prompt="A mixed metaphor combines two comparisons that do not fit together.",
                            choices=("True", "False"),
                            correct_index=0,
                            explanation="It is usually unintentional and reads as a mistake.",
                        ),
                    ),
                ),
            ),
        ),
    ),
)

#: The published curriculum, in the order a learner should meet it.
CURRICULUM: tuple[SeedLevel, ...] = (_A1, _A2, _B1, _B2, _C1, _C2)
