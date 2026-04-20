-- update.applescript
-- Update an existing reminder by id.
-- Usage:
--   osascript update.applescript <id> <field> <value>
-- Fields:
--   name      — set name to <value>
--   body      — set body (notes) to <value>
--   due       — set due date to <value> (ISO YYYY-MM-DDTHH:MM:SS); empty value clears
--   complete  — set completed to <value> ("true"/"false")
--   move      — move reminder to list named <value>

on run argv
    if (count of argv) < 3 then
        error "usage: update.applescript <id> <field> <value>"
    end if
    set rid to item 1 of argv
    set fld to item 2 of argv
    set val to item 3 of argv

    tell application "Reminders"
        set target to missing value
        try
            -- AppleScript exposes id as `x-apple-reminder://<uuid>`; the
            -- caller passes the bare UUID (as returned by reminders-cli's
            -- externalId field), so we substring-match.
            set target to (first reminder whose id contains rid)
        end try
        if target is missing value then
            error "reminder not found: " & rid
        end if

        if fld is "name" then
            set name of target to val
        else if fld is "body" then
            set body of target to val
        else if fld is "due" then
            if val is "" then
                try
                    set due date of target to missing value
                end try
            else
                set d to my parseIso(val)
                if d is not missing value then set due date of target to d
            end if
        else if fld is "complete" then
            if val is "true" then
                set completed of target to true
            else
                set completed of target to false
            end if
        else if fld is "move" then
            try
                set targetList to list val
                move target to targetList
            on error errMsg
                error "cannot move to list '" & val & "': " & errMsg
            end try
        else
            error "unknown field: " & fld
        end if
        return "OK"
    end tell
end run

on parseIso(s)
    try
        set y to (text 1 thru 4 of s) as integer
        set mo to (text 6 thru 7 of s) as integer
        set da to (text 9 thru 10 of s) as integer
        set hr to 0
        set mi to 0
        set se to 0
        if (length of s) >= 13 then set hr to (text 12 thru 13 of s) as integer
        if (length of s) >= 16 then set mi to (text 15 thru 16 of s) as integer
        if (length of s) >= 19 then set se to (text 18 thru 19 of s) as integer
        set d to current date
        set year of d to y
        set month of d to mo
        set day of d to da
        set hours of d to hr
        set minutes of d to mi
        set seconds of d to se
        return d
    on error
        return missing value
    end try
end parseIso
