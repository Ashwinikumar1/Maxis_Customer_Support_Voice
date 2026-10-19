# Overview
The team met to define the architectural framework, infrastructure strategy, and development roadmap for a new agentic system. Key decisions were made to adopt an Orchestrator Model using sub-agents for scalability and to host the solution on Cloud Run with pre-warmed instances. The discussion also covered critical requirements for PII sanitization, human handoff mechanisms via a pull-based API, and a two-week development sprint starting with a dummy system integration.

# Details
* **Orchestrator Architecture**: The team decided to use a root agent managing sub-agents as tools to ensure scalability and maintain context during topic switches.
* **Infrastructure Strategy**: The system will be hosted on Cloud Run using multiple pre-warmed instances to manage high concurrency and minimize latency.
* **Intent Engine Design**: FAQ handling will be integrated directly into the intent engine to minimize latency for frequent, simple user queries.
* **Latency Optimization**: To achieve a 1–2 second response target, the team plans to reduce chunk sizes and limit search results.
* **Data Privacy Measures**: The team will sanitize data using STP before storage and requires customer clarification on specific PII reduction methods.
* **Human Handoff Mechanism**: A pull-based static API was selected to allow customers to retrieve summaries and transcripts for human agent transitions.
* **Development Roadmap**: Week 1 will focus on a dummy system to test functionality, followed by Genesis integration in Week 2.
* **Observability Plan**: The team will integrate OpenTelemetry to capture all system data and establish basic health monitoring.
* **Ownership and Expertise**: The team identified a need to address ownership conflicts and will model the Genesis integration after previous GCS interventions.
* **Testing Challenges**: Due to provider variability, the team must research audio connectors to simulate connections independently of the Genesis system.



# Full Transcript

Transcript 14:40
Microphone: Right..
Microphone: So, and my understanding is that
Microphone: In the starting of the conversation it is very very clear whether it is a Q or it is a rolling flow. Right. Yes. Do you think there would be scenarios where someone is in the starting
Microphone: You mean when the first Yeah, when the first communication happens. The, right? the part. I think we are not sure about that. Yes. So I think we need to have a conscious mechanism. We we will we will come to the
Microphone: will come to the actual gut, right? But so should we design it in a way that people can interleave between conversation, which basically means that you are having a looming conversation, but suddenly you can have an efficution and
Microphone: . Yeah. I think yeah, possible. You should be able to switch in between, right? Yes. Right. Because then it changes the complete architecture of the ADK and everything. Yeah. Yes. Right.
Microphone: Okay.
Microphone: So, there are two ways of approaching this problem statement. So, the first way is that
Microphone: What we could have done is we could have had an orchestrator, is it? Right.
Microphone: And this agent
Microphone: will have access to I think.
Microphone: Okay.
Microphone: in the root agent, you mean.
Microphone: Strategian, the orchestrator Strategian have access to the. Yes.
Microphone: I'm just thinking out loud. It's not the final one. Obviously we'll discuss the trade off. everything has a trade off. Got it. Right? And then we have the sub agents roaming. Yeah. Right?
Microphone: A woman will have his own instruction, its own tool call and everything. Okay. Right? Share context or isolated context. share. Okay. So that basically even if it knows that if it doesn't answer a question, it is able to go.
Microphone: two doll is there isn't an effect you can answer it. Okay. Right?
Microphone: So there are few things which we need to clarify need to test out clearly. I know it works that when I you question comes it is able to go there and all those things. And the advantage of using this kind of an architecture is
Microphone: It is highly scalable. What I mean is that you can have everything working and you can just add another reason for fiber and you can have another reason for lyxide.
Microphone: They thought I don't remember what it saying, right? Right.
Microphone: So you can keep adding to and the latency will not increase because it is just one decision it is taking and it is transferring the context. Right. The only thing which worries me right now is that it introduces
Microphone: a latency here. Between the orchestrator and J and the sub agent, right? Yes. Yeah. It introduces a little bit of latency here. All right. So that is something which we have to figure out. All right. So that is one architecture, right? The second way
Microphone: is if they want roaming and we are not thinking about scalability, let's make it very, very simple.
Microphone: All right. Very simple basically means root isn't. Yeah. Has access to all the APIs and specs all the and all those everything. Yeah, but it will not scale. But from a longer term
Microphone: right? I think they want to go back to CX, right? This is the interim solution, right? ADK.
Microphone: But the other thing is we can actually work on both because if we have the orchestration and everything working. If the legacy, you know, is is super small and electrical, right? Yeah. At one because we can say that
Microphone: Yeah. But would it actually scale, you think? From a customer background? From the customer perspective, they really they haven't really calculated the cost. Okay. For the.
Microphone: . But it would be higher than. It's super higher. We already told him, right? It might be, you know, three times higher than your X. Right. This would be this would be super cheap. Yes, correct. And then when we introduce the hosting.
Microphone: . Yeah, makes sense.
Microphone: So they might be able they might use this as a main. Okay. So, if we are agreeing on this, we can keep this as like a dummy dummy thing.
Microphone: just to compare latency on both side, right? But we put forward this. Yes. Unless we face a major push back from that that the latency is too high and all those stuff. We keep on going ahead with this. Right. Okay. So are we on
Microphone: Yeah. I I like the orchestration as well because even if the sub agent has a latency here, we have an opportunity to do a place holder, right? Or let me work on that. that that part can come from the orchestration. So it would be before to call back and I
Microphone: talk about the architecture in detail. Right? So let's talk about the architecture right. Yeah, because I think one thing we don't have to solve the net latency. We just have to improve the user experience. Okay. So I'm not talking about Genesis integration at all right now.
Microphone: We'll worry about that later. We start with the
Microphone: So sit down..
Microphone: We start with the cloud. Right?
Microphone: cloud run URL.
Microphone: Okay. When we have this cloud run URL. So I'm just talking about this, but we'll have let's say two instances running and we'll schedule it to 10, 15 also on and so forth. What I know is depending
Microphone: on their latency, you mentioned that they need 300 concurrent calls. Cloud can handle 50 to 70 concurrent one cloud can handle that. So if we have 10 15, it should be able to handle that. Okay. So this is something which I've tested before.
Microphone: vice. Yeah. What about uh scaling latency? Uh so we'll pre-warm it. So don't worry about that. Yeah.
Microphone: minimum.
Microphone: . Yeah, what we'll do is depending on their load and all those stuff, what we'll see is if the if the CPU utilization is around 50% only, then we start opening new clouds and all those stuff.
Microphone: Yeah. Okay. And these are very very small instances of cloud and not very heavier ones. Okay. So this is cloud and URL. Yeah. All right. So
Microphone: This this is just the interface which we will provide to the user, right? Okay. So this interface is a audio interface? Yeah, it would be an audio interface. right? So actually, let me just simplify this for you. So Genesis
Microphone: will be here. I'm not talking about it, but Genesis what it will send is MSISPN.
Microphone: and audio input.
Microphone: to what? socket, right? And when the web socket receives it, it will start a session. Yeah. The session will use this as an identifier and this will flow throughout our ADK. Right? And
Microphone: and the audio input will flow. All right. Now, the architecture, I'll I'll talk about the ADK architecture. Let me know if you have any questions on this. Okay. Yeah, I think one of the the conversation profile from from my group.
Microphone: the microservice from the Genesis, they not just MSN, they also have this Genesis profile which identify
Microphone: conversation.
Microphone: Which I Okay. So maybe I think tomorrow we clarify also. Yeah. Because I think that was falter when you pass it to an agent.
Microphone: human agent. So human agent will be looking at that on the go.
Microphone: . Right? So, I'm not sure, but we'll clarify that. Right.
Microphone: So, I I'll just draw the editing architecture more properly here now. Now, this will cloud and what it will do, it will send audio. So audio is by default, right? But it will start a new session.
Microphone: And the session and it will start a user ID, let's say we use user ID or MSID ID. User ID would be a
Microphone: this MS MS MS.
Microphone: Okay.
Microphone: No.
Microphone: It comes to our
Microphone: street.
Microphone: agent. Okay.
Microphone: So this is also our intent engine. When I say intent engine
Microphone: It basically means that it is able to understand what is the intent of the user. Right. And able to answer that. So
Microphone: I'm I'm leaning towards and I need your guys input. I'm leaning towards having FAQ directly inside it because it is just a simple tool and it will never confuse there. Right? So, and the prompt would be something like that if you get questions about roaming
Microphone: go to this agent, this agent, but if you think it is a general purpose answer, just call the FAQ and answer it. Right? Because FAQ is something which would be asked very, very often. So we just want to make sure that the latency, the passive latency is very, very.
Microphone: on that, right? So, this will have
Microphone: So it will just have access to vertex AI.
Microphone: such.
Microphone: Right. And it will do the summarization in everything by itself and just share it of course. Okay. And here I think because voltex CI search latency is around 100 at max 100 millisecond also. So we should be able to get an answer in one second or two seconds.
Microphone: in most cases. Correct. But just on his demo, right? Just now he said that such is the one that giving the
Microphone: like. No, it should not be the case. I think there's some optimization which need to happen. Right? Maybe we will will reduce the will reduce the chunk size and or also because it is an FAQ document which is very, very clearly defined.
Microphone: We don't need to send top five or top 10. We may just get restricted to top one and two because they need very specific answers. Okay. Okay. Yeah.
Microphone: that that's one of the concern. We need to optimize that. Yes. So but this is how it will look like. Yes. Agree. Agree. Right. Now it will have access to
Microphone: multiple agents, right?
Microphone: So I'm crossing this right now just because this is not in scope right now. But we just want to highlight this to our customers that this will have access and everything. Correct. Right?
Microphone: So we have the
Microphone: . Okay.
Microphone: And one thing which I want to highlight, I think this is where a lot of these agent system fails, right? Is the fact that
Microphone: So, you would have heard the term of using agent as a tool and using sub agents, right? Yeah. The problem with that is so if you use it as a tool
Microphone: You might have double step roaming answer is ansing and orchestrator is summarizing. Right. But if you use it as sufficient and roaming is it will answer. But the problem is that if we ask something which is related to fiber. Move. It will not be a
Microphone: context, right? So, I think that needs to be tested, but for now we'll have it as isn't as a
Microphone: tool. Okay. Right. So it would be a sub like it would have its own prompt and everything, but will use it as a tool. Okay. Right. Also, I think ADK2 this has improved, right? So it is now able to
Microphone: actually ensure that it always transfers back to orchestra and all those stuff. So we need to change that methodology, but it should work. Right. Yeah, and uh from the like and say perspective this is also faster, right? Yes. Okay.
Microphone: Yes. Okay. All right. So, now when we come to the roaming agent or sorry, sorry on the agent we call your agent as well would the orchestra agent still be able to give a pre call response? Yes. Like let me
Microphone: Yeah, before two call back. Yeah. Right.
Microphone: So now promism light
Microphone: So, it will have access to a lot of tools, right?
Microphone: a lot of tools. So that's what you get away right there. Yeah, yeah. Open eyes picks which they have shared, right? And every tool call would have depending on the requirement of before.
Microphone: school.
Microphone: call back..
Microphone: Right? So what is this? This is very simple that I'm fetching some information for you or something like that which is used as a filler to basically do that. Right.
Microphone: Open. Open.
Microphone: Hey.
Microphone: So this is the complete architecture we are looking at. And this is scalable, right? How I say it is scalable is because you are just starting an instruction here that you call the roaming agent when the question is regarding roaming maybe just two three lines. Yeah. Which basically means when I want to do the same
Microphone: for a fiver. I can create a fiver agent independently. And just add two three lines that call the fiver agent and make it available to our organization. Okay. Okay. Right. And the other beauty like
Microphone: The other thing which I'm realizing is that if we don't have like hundreds of agent, if we only have four or let's say five seven 10 flows, it will not impact the latency because we'll just be adding 10 20 lines in the.
Microphone: Does that make sense? sense. Right? Now the inner details, right? whether the state region would be a coordinator or something like that is something which I need to figure out. Right. The only thing which we can't
Microphone: to the customer is that live doesn't support parallel agents.
Microphone: And it doesn't support blue patients.
Microphone: So what does it support? Gemini life. So we have to create. Yeah. And if it doesn't support here, it doesn't support GC itself. For sure.
Microphone: Okay. Now what we have got from So this is the architecture, right? And we'll build around. Yeah. So one more consideration you mentioned was the data reduction. Yes. Now we'll come to all those one. So, I think one thing which I've tested already is that
Microphone: when the conversation is going on, when the conversation is going on. So we are able to
Microphone: saved a complete transcript in the S3 bucket. So I I'll come to that, right? S3 or GCS. Yeah. GCS. Okay. The GCS bucket, right? And the reason for that is we
Microphone: We are not showing this to Wiki as of now. right? But internally we want to have a mechanism where all the transcript are scored. So that if you want to do sentiment analysis, if you want to do a summary and all those stuff, you are able to do it later. Right? So this the swag
Microphone: understand this. Okay?
Microphone: Now the things which are pending, right?
Microphone: How do we do the PI? So when you say PI rejection, do we understand from the customer what does it mean? So they don't want any PI information going to
Microphone: going to the our LLM models. What does it mean?
Microphone: we haven't got any definition, right? As of now what's considered as PII uh from them, but what we can do, I think we can say that common PII maybe.
Microphone: ID number. ID number. That's the. Yeah, yeah, but there is no Yeah, in this
Microphone: How do we get that information in this? tomorrow we can assume. But right now we can assume, for example, we can get the BI. Yeah using our. The other thing which we also need to understand from the customer is
Microphone: What does PII reduction mean? They don't want to PII Is it masking? Is it masking? removing or
Microphone: or right. Yeah, because if you look at it, right, I can have I could say that when whenever the is talking, it should not
Microphone: talk about any PII information, right? But then the ist is only talking to the user ID which is authenticated. So even if I tell you your name and email address ID, that should be fine. Because we are talking about Yeah, yeah.
Microphone: his or her PII only, right? I think the only consideration they don't want their PII of the user to go to the model. Or into the storage bucket. Into the storage bucket. Yes.
Microphone: So usually, yeah, usually they are I mean most of my customers are okay with being memories, yeah. Yeah. Yeah. But if anything writes to this. Yeah. So that is where I think we have to put a little bit more thought, right?
Microphone: We can always sanitize that with, you know, STP before that. Yeah, yeah. We can do that. Okay. So because it is Gemini transcript, right? So I'm just thinking there should be a way. Otherwise we could have one version in the staging and then whenever we are
Microphone: scraping the data like we'll be moving it to big query or somewhere. We just sanitize it. Yeah. Yeah. So this one we we going to deploy the agent into agent runtime or runtime. Cloud runtime runtime runtime doesn't support live, right?
Microphone: not sure. That was I haven't checked yet. Yeah, so last time it did not. Okay. Yeah. But that is last time was six months back. So things changed very fast. Okay. Yeah, the one that I deploy is also. Yeah.
Microphone: Because yeah, if we put it in the runtime then, you know, the monitoring etc, right? that one should be really easy. Yes, it is easy.
Microphone: Plus it's quite stable also. If you have enough instances and all those stuff, it is quite stable.
Microphone: night.
Microphone: So and ADK has a mechanism to disconnect and everything. So even if like web sockets are not very like they are brittle sometimes. So it has a mechanism to reconnect and all those stuff also.
Microphone: I know people the last live in which I created, I was able to hold conversation for 20 minutes. Okay. Yeah. Okay.
Microphone: I think it's fine. Yeah. So here we don't need to but there was session assumption, context memory and all those stuff. All right. The what are the other things which we are missing in this agent? It makes it looks too easy right now.
Microphone: . Yeah, the reason is also the scope down this one and then
Microphone: And then they are prioritizing on the delivery time. Yeah. So it's good like just make it easy first. Yeah but And then pass and streams easy it doesn't mean that it's not something
Microphone: which is not like well thought through I think. Well well think it through properly. Yeah think about how to, you know, to to hand over to the human. Yes, that is the critical part. Yeah. So it would be a tool call, right? What I
Microphone: worried about is when we do that tool call, how do we send him the transcript?
Microphone: Right? Before all tool call back, we need to write it to Genesis right? And then Genesis we need Genesis API. I think we should make a vote. So if we were talking about is in
Microphone: is in
Microphone: transfer to humanism.
Microphone: for humanization.
Microphone: We need two APIs here. The first API is
Microphone: summarize and transcript to Genesis cloud. We have yeah. Yeah. And the second would be the tool called which actually transfers the audio from cloud run to directly to the output. How does that transfer work?
Microphone: ight.
Microphone: I I don't know what is the term for that, but you understand? Yes. Yeah, so I I think we need to make it simple right. the how we we transfer to human agent, right? We have two mechanism. The first one is polar. polar mechanism
Microphone: right from the human agent just to take all these summarize and etc. right? So they will get a pull, we we create the the the API for example, right to for them to pull. And then the second one is push.
Microphone: post means whenever we transfer, right? we transfer then we post this this information and the summary to their API which we don't know yet as of now. Yeah. So yeah Poland push
Microphone: the same thing yet, no? I mean full full yeah, we have this with with API that's what I meant. That that human will already have right?
Microphone: in our system, in our system, we have a full
Microphone: they can pull the data from our system. Which system? Genesis? Okay, but then we need to push it somewhere before they can pull it, right? Because it's all in memory right now.
Microphone: I I get what you're saying. So yeah, so for for that case, right? Genesis you can have multiple destinations. Yes. We can we can trigger. So we need to trigger two things. Okay, so in time. Yeah. Then we can say but this is the one that we need to ensure.
Microphone: So, when we would like to transfer
Microphone: Okay..
Microphone: transfer to human, transfer to human.
Microphone: we need to do two things. Okay. The first one is we need to tell this one is a
Microphone: Genesis.
Microphone: genesis. Okay, move to human.
Microphone: also human. No audio needs to be transferred in here. Just tell the Genesis, okay, this this line needs to go to the human. and then it will go to the Genesis.
Microphone: Genesis and Genesis.
Microphone: Genesis. This one goes to Genesis and Genesis. The second one is tell right, tell the API over there, right? Genesis or Genesis.
Microphone: engine series.
Microphone: about
Microphone: summary. Yes. summary and transcript. Yes. All right, transcript.
Microphone: Okay. This one either the Genesis as the API. Yes. API Genesis or said was he would like to modify thesis
Microphone: agent assist desktop with the URL, the desktop, the desktop
Microphone: to be able to show this without
Microphone: getting the information from. Yeah, so there also will need an API or something. Yeah, yeah. This one we need to find out how can we push the information Yes. to Genesis and how can we push the information? we are saying the same thing. Yeah, or the third thing, this is the one
Microphone: that that that uh what I was saying about pool. Uh that the thing is we provide
Microphone: provide static API.
Microphone: API we provide because if we are unable to connect to this one Yes. Yeah, we just provide API and let them
Microphone: I connect to this one. This is the same as the second one, right? This one is we are posting. We have they have their API. They have their API. We are contacting to their API. This one they have their API. We are contacting. So the first one is the Genesis API, the second one is maybe the
Microphone: Correct. Yeah. This one what I say post they post I post to them and this one they pull. sense. Makes sense, yeah. Because we this is relying a lot on them, right? We we don't have any control. But this one we have a control.
Microphone: If they are asking where's the summary, where's the transcript? Hey, we already have the API for you to get this.
Microphone: just give them something that we can control. Makes sense.
Microphone: Should we take a photo of this and convert this into. Yeah. And then in here also, I think we need to have this.
Microphone: We need to have this research, right? no need to put it. We need to have this research about the the connector audio connector. The one that how can we simulate this and how can we connect without without we are connecting to the genesis.
Microphone: less of now because we don't have, right? Maybe we have a But I think we can there are to you and other things which you could do that right? But the problem is that
Microphone: . Yeah. That's completely. why are you Oh well. Oh well. Because he's still complex. Yeah yeah yeah. Yeah yeah. It's okay don't worry. I have a transcription of my.
Microphone: . No, he would like to take a picture. No, no, no, no problem. No problem. So I think we could do that. The only problem is that that mechanism will completely change.
Microphone: What do you mean? Sorry, what do you mean? Every cloud provider, every So to know is one of the telephone providers. other telephone the mechanism would be a little different. Okay. Yeah. So I think but the point remains still valid.
Microphone: Okay, how do we test it out? Yeah, yeah. Right. So for ADK, I know how to test it out. Yes, yes. Right? Once we have access to their API when we are starting the session, we can pass in the dummy which would actually be there. Right? And then this one is the one.
Microphone: Yeah. That's hard to test. Yes.
Microphone: Yeah, we need to think about this. That one, yeah, makes sense on how. But right is we have everything from them, right? So we can have the first out within a week. Right? Yeah. And Yeah. But understand like like said, right?
Microphone: person is given that person is super hard to work with.
Microphone: Right, so it means that he will going to do if like like said, if we give him the step by step guidance on how to configure over there, then he will going to do it. But if he we ask him to
Microphone: do some research. you're not going to do it. Understood. understood. And what I'm saying is let's after agent running
Microphone: right in a week's time. Okay. Right? And then from week two we start the Genesis integration. that's what I meant because it's Can we do it parallel in week one?
Microphone: But if you don't have Nothing to be tested. Yeah, nothing to be tested and then what I'm saying is
Microphone: Let's have uh dummy Asian working, right? Which will mimic what we have. Okay. Okay. Okay. And even if and then we start the genetic. Okay.
Microphone: Okay. Also like you might have to talk to Dani about the ownership because she like Yeah the customer said he's the right? So I think the ownership
Microphone: The already decided the. what I mean is on the field experience is different. Yeah. Yeah, yeah. So but just because I know that there will
Microphone: have concerns with that because she has like clearly called that out even that yeah. Yeah. So maybe we can
Microphone: we can start early on that one, so we can identify the issue.
Microphone: Genesis. Do you have any experts on that? Yeah.
Microphone: Even our team I think that's the only unknown, right? The only unknown and we need to have a mechanism to test that by ourselves. And then give a clear guidance to them, hey, we need you to do this, this, this, this, this.
Microphone: I think it would be very similar to how it has been done for GCS. Yeah. Yeah, it would be very, very similar. Yeah. Can we also have the GCS person on the call from Google? Who who actually guided them to the intervention? No, they are not guided by our GCS.
Microphone: Okay, they're not by themselves. Okay. So maybe they're engineer who actually like what are the specs they share, how did they share it? Otherwise we will do the research.
Microphone: Oh, sorry.
Microphone: What else we need to discuss? We need to convert this into an architectural. Uh what about observability? Observability is inbuilt in the. Yeah, but you should we like push it to your GCS currency so they can Yeah or give a monitoring or
Microphone: Yeah. how are we going to Just have a perspective on that. Yes. So I think the thing here would be because it is not with it comes by default. Yeah. All right. So because it is cloud.
Microphone: So we have to add our open telemetry and all those stuff. Okay. And yeah my my my thinking is uh we do two things for this ability. The first one is we create the the spot for the part run which is
Microphone: using the car.
Microphone: go up one so that's. And they cut monitoring.
Microphone: And with that as a the happiness of the system. Yeah. And then um I don't know whether we should create a separate.
Microphone: I think keep it simple first. So let's let's let's make sure at least that we are creating. Okay. So that every data is being captured. Now how do you want to consume that data is
Microphone: . All right. So how to start the