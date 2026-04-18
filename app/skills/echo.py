from app.skills.base import SkillRequest, SkillResult


class EchoSkill:
    name = "echo"

    def run(self, request: SkillRequest) -> SkillResult:
        text = str(request.args.get("text", ""))
        return SkillResult(ok=True, exit_code=0, stdout=text, summary=text)
