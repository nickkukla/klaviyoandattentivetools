Here is a light spec for the klaviyo and attentive tools I'm looking to build.  Note, there is one other integration I need, which is a STOQ app integration.  

I'm running a migration, and part of that is merging various objects and records for Klaviyo and Attentive.  

The source Klaviyo and Attentive instances are the Canada instances, which I will refer to as either klaviyo_ca, or attentive_ca.  The destination instances are the corresponding US versions of each instance, which I'm referring to as klaviyo_us and attentive_us. 

I need to stand up a tool that will allow me to, from CLI or asking Claude Code to invoke it from a Claude Code session.  The specific actions I need are described below. 

Please base what you build on the documented public api for Attentive (reference found here: https://docs.attentive.com/reference/test-authentication-v2), and Klaviyo (api reference here: https://developers.klaviyo.com/en/reference/api_overview). For STOQ, you should use this reference (https://docs.stoqapp.com/)

For attentive, I will need the following actions: 

Attentive Segments:
* I need to be able to export and save to a local csv file all Segments, including the Profiles attached to each Segment at the point of export.  
* I need to be able to migrate Segments from attentive_ca -> attentive_us.  The migration options should be either just Segments or Segments and Profiles.  It's not clear from the Attentive documentation (to me) if you need to pull segment membership for each profile, or if you can hit the API for all profiles under each segment.  That needs to be determined.  
* I need to be able to pilot or dry-run the Segment migration in the last bulletpoint.  The pilot mode should accept a specific Segment name or ID, and perform the migration for ONLY that segment.  The dry-run mode loads the data, but does no writes, it just explains what would have been migrated. 

Attentive Campaigns
* I need to be able to export and save to a local file all Campaigns, along with any Audiences, Messages, attached to those Campaigns.  This can be stored in whatever format you think is best.  I should be able to run this against either the attentive_ca or attentive_us instances. 

Attentive Lists
* I need to be able to export and save to a local file all Lists, and profiles attached to those Lists.  This can be stored in whatever format makes the most sense to you.  This should be able to run against the attentive_ca and attentive_us instances. 

Attentive Profiles
* I need to be able to export and save to a local file all Profiles in the , with all data available from the Attentive API.  This can be stored in whatever format makes the most sense to you.  I should be able to run this against either attentive_ca or attentive_us.  

Attentive Catalogs
* I need to be able to query each instance, and request a list of the catalogs, which can be displayed in console.  

Attentive Catalogs 
* I need to be able to export and save to a local file all Coupons.  This can be stored in whatever format makes sense to you.  THis should be able to run against either attentive_ca or attentive_us.  

For Klaviyo, I will need the following actions: 

Klaviyo Profiles: 
* I need to be able to export and save to a local file all Profiles with all fields available from Klaviyo (especially consent state, consent timestamp, consent source, last order date, local, language, and location, among the other fields.  This should be be csv format.  This export should be able to be run against both klaviyo_ca and klaviyo_us.  
* I need to be able to import to klaviyo a local file of profiles.  This should accept all of the fields available from Klaviyo profile api.  I should be able to issue this import action against either klaviyo_us or klaviyo_ca. 
##note, for klaviyo profiles, the intent is to manually pull down lists of profiles in both klaviyo instances, dedupe them, and upload ONLY the unique CA profiles to the US instance, preserving all the consent and opt-in info.  I'd like the tool we are building to be able to do this in either direction, but the project intends to only use this one-way for now. Also, the dedupe process will respect most recent consent datetimes, we won't update more-recently consented date/times on the profiles.  The dedupe process is outside of this application, so don't add any functionality to assist with deduping.  
* If it is possible, I'd like to be able to export a list of Profiles by Segment.  Same export requirements as above (csv, applicable to either instance). 

Klaviyo Lists: 
* I need to be able to export all Lists and their current Profiles.  This can be stored in whatever format makes sense for you.  

Klaviyo Back In Stock: 
* I need to be able to export and save to a local file all Klaviyo Back In Stock subscriptions.  These will ultimately be uploaded into the STOQ app, so please use this reference to help build the export (https://help.stoqapp.com/back-in-stock/migrate-klaviyo-back-in-stock-signups/).  please make sure the export contains all fields required for the eventual STOQ upload. 

Klaviyo Segments: 
* We are intending to use the "Clone" action within Klaviyo UI to move segments from instance to instance, but I would like to be able to export all Segments and point-in-time profile membership in those segments.  I'd also like to include, on the export, an indication if the Segment is based on any of the following categories of definitions: [Engagment metrics (e.g. Opened email in last week, Ordered in the last 30 days), Site Activity Metrics (clicked on X product or category), or 3rd party app events (e.g. "RSVP'd to Eventbrite).  I'm planning to create temporary segments on the destination/merged klaviyo instance, as engagement and activity stuff gets gradually rebuilt.  

For STOQ, I will need the following action: 
* I need to be able to import a set of Back in Stock subscriptions into the STOQ app, based on the instructions provided in the STOQ reference above in Klaviyo Back in Stock.  The source will be a csv.  I'm fully able to manipulate the CSV as needed to support STOQ import, please include any instructions in a read me.  

I believe that is it.  

For interacting with the app, I'm envisioning a command line app I can call with complex arguments so I can invoke a particular action against a particular instance.  Please include a read-me file that details all the possible actions and arguments.  

I'm only interested in the dry run mode for the migration task in Attentive Segments, I do not need that elsewhere.  For all other records, I plan to either just export (for archival purposes), or export the records out of one instance, manipulate or check the data, and import into the corresponding instance.  



