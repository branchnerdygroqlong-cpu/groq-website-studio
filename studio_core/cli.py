import argparse
from .orchestrator import ProjectOrchestrator
from .agent_runtime import WebsiteAgents

def main():
    parser=argparse.ArgumentParser(description="Groq Website Studio — Phase 1.5")
    commands=parser.add_subparsers(dest="cmd",required=True)
    new=commands.add_parser("new",help="Create a project")
    new.add_argument("name"); new.add_argument("--type",dest="business_type",default=""); new.add_argument("--brief",default="")
    commands.add_parser("projects",help="List projects")
    task=commands.add_parser("task",help="Add a project task")
    task.add_argument("project_id",type=int); task.add_argument("title"); task.add_argument("--description",default=""); task.add_argument("--agent",default="")
    status=commands.add_parser("status",help="Show project status and tasks")
    status.add_argument("project_id",type=int)
    build=commands.add_parser("build",help="Run Project Manager, Image Sourcer, and Frontend Developer")
    build.add_argument("project_id",type=int)
    build.add_argument("--brief",required=True,help="Website requirements for this build")
    args=parser.parse_args(); app=ProjectOrchestrator()
    if args.cmd=="new":
        p=app.create_project(args.name,args.business_type,args.brief); print(f"Created project #{p['id']}: {p['name']}\nFolder: {p['root_path']}")
    elif args.cmd=="projects":
        rows=app.store.list_projects()
        print("\n".join(f"#{p['id']} [{p['status']}] {p['name']} — {p['root_path']}" for p in rows) or "No projects yet.")
    elif args.cmd=="task":
        print(f"Created task #{app.add_task(args.project_id,args.title,args.description,args.agent)}")
    elif args.cmd=="status":
        result=app.project_status(args.project_id); print(f"{result['project']['name']} [{result['project']['status']}]")
        for t in result["tasks"]: print(f"  #{t['id']} [{t['status']}] {t['title']} — {t['assigned_agent'] or 'unassigned'}")
    elif args.cmd=="build":
        project=app.store.get_project(args.project_id)
        if not project: raise SystemExit(f"Project #{args.project_id} not found.")
        result=WebsiteAgents(project,store=app.store).run(args.brief)
        print(result)
        print(f"Project files: {project['root_path']}")
